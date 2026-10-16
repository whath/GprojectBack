"""Finite, resumable historical initialization, independent of the disabled worker."""
import json
import time
from datetime import date
from pathlib import Path

from . import db
from .calendars import validate_collection
from .history import sessions, audit
from .normalize import PendingData
from .pipeline import Collector, CollectorLock
from .provider import AKProvider


def catalog_coverage(day, cfg, count=30, kinds=('stock', 'etf'), scope='catalog'):
    expected_days = set(sessions('CN', day, count, cfg))
    with db.connect() as con:
        instruments = [dict(r) for r in con.execute('''SELECT i.*,l.listed_date FROM instruments i
            LEFT JOIN instrument_lifecycle l ON i.id=l.instrument_id WHERE i.market='CN'
            AND i.kind IN ('stock','etf') AND COALESCE(l.status,'listed')='listed' ORDER BY i.id''')]
        actual = {}
        for identifier, stamp in con.execute("SELECT instrument_id,trade_date FROM bars WHERE trade_date>=? AND trade_date<=? AND adjustment='none'", (min(expected_days), day.isoformat())):
            actual.setdefault(identifier, set()).add(stamp)
        suspended = {}
        for symbol, stamp in con.execute("SELECT symbol,trade_date FROM datasets WHERE dataset='suspensions' AND market='CN' AND trade_date>=? AND trade_date<=?", (min(expected_days), day.isoformat())):
            suspended.setdefault(symbol, set()).add(stamp)
    configured = {'CN.' + kind + '.' + symbol for kind in kinds for symbol in cfg.get('cn_' + ('stocks' if kind == 'stock' else 'etfs'), [])}
    items = []
    for item in instruments:
        if item['kind'] not in kinds or (scope == 'configured' and item['id'] not in configured):
            continue
        expected = {stamp for stamp in expected_days if not item['listed_date'] or stamp >= item['listed_date']}
        absent = expected - actual.get(item['id'], set()) - suspended.get(item['symbol'], set())
        items.append(dict(item, missing_count=len(absent), complete=not absent,
                          observed_count=len(actual.get(item['id'], set()) & expected),
                          first_required_date=min(expected) if expected else None))
    return dict(trade_date=day.isoformat(), window=count, scope=scope, expected=len(items),
                complete_count=sum(i['complete'] for i in items),
                incomplete_count=sum(not i['complete'] for i in items), items=items)


def run(cfg, day, count=30, max_items=100, budget=600, kinds=('stock', 'etf'), scope='catalog', raw_dir=None):
    validate_collection('CN', day, cfg)
    began = time.monotonic()
    before = catalog_coverage(day, cfg, count, kinds, scope)
    settings = dict(cfg, history_sessions=count, history_days=(day - date.fromisoformat(sessions('CN', day, count, cfg)[0])).days,
                    collection_budget_seconds=budget, request_timeout_seconds=30, request_attempts=1)
    delegate = AKProvider(settings)
    folder = Path(raw_dir) if raw_dir else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    class Recording:
        def call(self, endpoint, **kwargs):
            rows = delegate.call(endpoint, **kwargs)
            if folder:
                (folder / (endpoint + '-' + kwargs['symbol'] + '.json')).write_text(
                    json.dumps(dict(endpoint=endpoint, kwargs=kwargs, rows=rows), ensure_ascii=False, allow_nan=False), encoding='utf-8')
            return rows
    collector = Collector('CN', day, settings=settings, provider=Recording(), refresh=True)
    collector.scope = 'manual_history_' + str(count)
    candidates = [i for i in before['items'] if not i['complete']]
    # Resume untouched entries before retrying broken ones. Attempts never activate a scheduler.
    with db.connect() as con:
        states = {r['task_key']: dict(r) for r in con.execute("SELECT * FROM tasks WHERE market='CN' AND trade_date=? AND scope=?", (day.isoformat(), collector.scope))}
    candidates.sort(key=lambda i: (states.get('bar:' + i['id'], {}).get('attempts', 0), i['kind'] != 'etf', i['id']))
    attempted = []
    with CollectorLock():
        for item in candidates:
            if len(attempted) >= max_items or time.monotonic() - began >= budget:
                break
            validate_collection('CN', day, cfg)
            source = cfg.get('cn_sources', {}).get(item['kind'])
            if item['kind'] == 'etf':
                endpoint = 'fund_etf_hist_sina' if source == 'sina' else 'fund_etf_hist_em'
            elif source == 'eastmoney':
                endpoint = 'stock_zh_a_hist'
            else:
                endpoint = 'stock_zh_a_daily' if item['source_code'].startswith('bj') else 'stock_zh_a_hist_tx'
            def action(item=item, endpoint=endpoint):
                collector.history(item, endpoint)
                coverage = audit(item['id'], day.isoformat(), cfg, count)
                if not coverage['complete']:
                    raise PendingData('historical sessions absent; listing/suspension evidence required: ' + str(coverage['missing_count']))
                return coverage['bar_count']
            collector.task('bar:' + item['id'], action, always=True)
            attempted.append(item['id'])
    after = catalog_coverage(day, cfg, count, kinds, scope)
    return dict(trade_date=day.isoformat(), window=count, scope=scope, attempted=attempted,
                status='complete' if after['incomplete_count'] == 0 else 'partial',
                expected=after['expected'], complete_before=before['complete_count'], complete_after=after['complete_count'],
                incomplete_after=after['incomplete_count'],
                note='finite manual slice; remaining inventory is not certified complete; rerun explicitly to resume')
