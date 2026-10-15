"""Read-only client readiness: implementation and stored coverage are separate facts."""
from datetime import datetime, timezone

from fastapi import HTTPException

from . import db
from .calendars import latest_ready


def report(cfg, now=None):
    now = now or datetime.now(timezone.utc)
    targets = {m: latest_ready(m, cfg).isoformat() for m in ('CN', 'US')}
    with db.connect() as con:
        latest = dict(con.execute('''SELECT i.market,max(b.trade_date) FROM bars b
            JOIN instruments i ON i.id=b.instrument_id WHERE b.adjustment='none' GROUP BY i.market'''))
        history = [dict(r) for r in con.execute('''SELECT i.id,count(b.trade_date) AS stored_bars,
            min(b.trade_date) AS first_date,max(b.trade_date) AS latest_date FROM instruments i
            LEFT JOIN bars b ON b.instrument_id=i.id AND b.adjustment='none' AND b.trade_date<=?
            WHERE i.market='CN' AND i.kind='stock' GROUP BY i.id''', (targets['CN'],))]
        snapshots = {r['feed_key']: dict(r) for r in con.execute('SELECT * FROM feed_snapshots')}
    funds = []
    from .funds import report as funds_report
    for symbol in cfg['cn_stocks']:
        identifier = 'CN.stock.' + symbol
        saved = snapshots.get('flows:' + identifier)
        if saved is None:
            item = dict(instrument_id=identifier, status='not_collected', latest_date=None)
        else:
            try:
                payload = funds_report(identifier, 2000)
                latest_date = payload['latest_date']
                item = dict(instrument_id=identifier, status='current' if latest_date == targets['CN'] else 'stale',
                            latest_date=latest_date, stored_rows=len(payload['items']))
            except HTTPException:
                item = dict(instrument_id=identifier, status='failed_or_invalid', latest_date=None)
            item['verified_at'] = saved['verified_at']
        funds.append(item)
    news = dict(status='not_collected', active_count=0, markets_present=[])
    saved = snapshots.get('events')
    if saved:
        try:
            from .events import report as events_report
            from .funds import aware
            payload = events_report(100, now)
            stale = (now - aware(payload['updated_at'])).total_seconds() > 24 * 3600
            news = dict(status='stale' if stale else 'available', updated_at=payload['updated_at'],
                        verified_at=saved['verified_at'], active_count=len(payload['items']),
                        markets_present=sorted({m for row in payload['items'] for m in row['markets']}))
        except HTTPException:
            news['status'] = 'failed_or_invalid'
    configured = {'CN.stock.' + s for s in cfg['cn_stocks']}
    from .health import coverage
    from .supplements import readiness
    return dict(checked_at=now.isoformat(), supplemental_feeds=readiness(), markets={m: dict(calendar_target_date=targets[m],
                latest_stored_bar_date=latest.get(m), stale=latest.get(m) != targets[m],
                scope='latest date among stored series; not whole-market completeness') for m in targets},
                configured_coverage={m: coverage(m, targets[m], cfg) for m in targets},
                cn_stock_history=dict(catalog_count=len(history), with_any_history=sum(r['stored_bars'] > 0 for r in history),
                    with_at_least_250_bars=sum(r['stored_bars'] >= 250 for r in history),
                    configured=[r for r in history if r['id'] in configured],
                    basis='stored unadjusted row counts; calendar completeness requires /v1/history-coverage/{id}'),
                configured_stock_funds=dict(expected=len(funds), current=sum(r['status'] == 'current' for r in funds), items=funds),
                news=news, scheduled_events_supported=False,
                source_limits=['THS complete dated membership unavailable', 'THS dated daily board funds unavailable',
                               'stock funds provider has a recent window, not a 2000-day backfill',
                               'keyword news is not a complete scheduled-event calendar'])
