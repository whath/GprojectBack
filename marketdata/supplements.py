"""Account-free reference data. Manual ingestion only; HTTP reads never collect data."""
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from fastapi import HTTPException

from . import db
from .calendars import CN, latest_ready, session_close, validate_collection
from .normalize import PendingData, day_string
from .settings import config

INDICES = {
    'CN.index.sh000001': dict(name='上证指数', market='CN', symbol='sh000001', currency='CNY'),
    'CN.index.sz399001': dict(name='深证成指', market='CN', symbol='sz399001', currency='CNY'),
    'CN.index.sz399006': dict(name='创业板指', market='CN', symbol='sz399006', currency='CNY'),
    'CN.index.sh000688': dict(name='科创50', market='CN', symbol='sh000688', currency='CNY'),
    'CN.index.sz399986': dict(name='中证银行', market='CN', symbol='sz399986', currency='CNY'),
    'JP.index.N225': dict(name='日经225', market='JP', symbol='日经225', currency='JPY'),
    'KR.index.KS11': dict(name='韩国KOSPI', market='KR', symbol='韩国KOSPI', currency='KRW'),
}
ZONES = {'CN': CN, 'JP': ZoneInfo('Asia/Tokyo'), 'KR': ZoneInfo('Asia/Seoul')}


@lru_cache(maxsize=8)
def reference_calendar(market, year):
    return xcals.get_calendar({'JP': 'XTKS', 'KR': 'XKRX'}[market],
                             start=f'{year - 9}-01-01', end=f'{year}-12-31')


def reference_close(market, day):
    if market == 'CN':
        return session_close('CN', day, config())
    cal = reference_calendar(market, day.year)
    stamp = pd.Timestamp(day)
    if stamp < cal.first_session or stamp > cal.last_session or not cal.is_session(stamp):
        return None
    return cal.session_close(stamp).to_pydatetime()


def reference_target(market, now=None):
    now = now or datetime.now(timezone.utc)
    if market == 'CN':
        return latest_ready('CN', config(), now)
    for n in range(31):
        day = now.astimezone(ZONES[market]).date() - timedelta(days=n)
        close = reference_close(market, day)
        if close is not None and close + timedelta(minutes=15) <= now:
            return day
    raise ValueError('no completed reference-market session')


def numeric(value, *, required=False, minimum=None):
    if value is None or str(value).strip() in ('', '-', '--', 'None', 'nan'):
        if required:
            raise ValueError('required numeric field absent')
        return None
    if isinstance(value, bool):
        raise ValueError('boolean numeric field')
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError('invalid numeric field')
    return result


def unique_rows(rows, field):
    if not rows:
        raise PendingData('empty provider response is unverified')
    if len({r[field] for r in rows}) != len(rows):
        raise ValueError('duplicate supplemental point')
    return sorted(rows, key=lambda r: r[field])


def save(key, metadata, rows, field='trade_date', replace=False):
    rows = unique_rows(rows, field)
    verified = db.now_iso()
    wire = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT metadata FROM supplemental_feeds WHERE feed_key=?', (key,)).fetchone()
        if old and {k:v for k,v in json.loads(old[0]).items() if k!='validation_report'} != {k:v for k,v in metadata.items() if k!='validation_report'}:
            raise ValueError('supplemental source/definition switch requires separate rebuild')
        con.execute('''INSERT INTO supplemental_feeds VALUES(?,?,?) ON CONFLICT(feed_key)
            DO UPDATE SET metadata=excluded.metadata,verified_at=excluded.verified_at''', (key, wire, verified))
        if replace:
            con.execute('DELETE FROM supplemental_points WHERE feed_key=?', (key,))
        con.executemany('INSERT OR REPLACE INTO supplemental_points VALUES(?,?,?,?)',
                        [(key, r[field], json.dumps(r, ensure_ascii=False, allow_nan=False), verified) for r in rows])
    return len(rows)


def saved(key, start=None, end=None, limit=2000):
    with db.connect() as con:
        feed = con.execute('SELECT * FROM supplemental_feeds WHERE feed_key=?', (key,)).fetchone()
        if feed is None:
            raise HTTPException(503, 'supplemental feed implemented but not yet collected')
        rows = con.execute('''SELECT payload,observed_at FROM supplemental_points WHERE feed_key=?
            AND (? IS NULL OR point_key>=?) AND (? IS NULL OR point_key<=?)
            ORDER BY point_key DESC LIMIT ?''', (key, start, start, end, end, limit)).fetchall()
    return dict(json.loads(feed['metadata']), verified_at=feed['verified_at'],
                items=[dict(json.loads(r['payload']), collected_at=r['observed_at']) for r in reversed(rows)])


def index_rows(rows, identifier, target, count=2000, quarantine=None):
    spec = INDICES[identifier]
    selected = [r for r in rows if day_string(r.get('date', r.get('日期'))) <= target]
    selected.sort(key=lambda r: day_string(r.get('date', r.get('日期'))))
    result = []
    unique_rows([dict(trade_date=day_string(r.get('date',r.get('日期')))) for r in selected[-count:]], 'trade_date')
    for raw in selected[-count:]:
        try:
            day = day_string(raw.get('date', raw.get('日期')))
            if reference_close(spec['market'], date.fromisoformat(day)) is None:
                raise ValueError('index date is not a local market session')
            if 'date' in raw:
                prices = {k: numeric(raw.get(k), required=True, minimum=0.0000001) for k in ('open', 'high', 'low', 'close')}
            else:
                if raw.get('代码') != identifier.rsplit('.', 1)[-1]:
                    raise ValueError('global index symbol mismatch')
                prices = {k: numeric(raw.get(v), required=True, minimum=0.0000001)
                          for k, v in [('open', '今开'), ('high', '最高'), ('low', '最低'), ('close', '最新价')]}
            if prices['low'] > min(prices['open'], prices['close']) or prices['high'] < max(prices['open'], prices['close']):
                raise ValueError('index OHLC bounds conflict')
            result.append(dict(trade_date=day, **prices, volume=numeric(raw.get('volume'), minimum=0),
                               volume_unit='source_unit_unverified' if 'volume' in raw else None))
        except ValueError as exc:
            if quarantine is None:
                raise
            quarantine.append(dict(trade_date=day_string(raw.get('date',raw.get('日期'))), reason=str(exc)))
    return unique_rows(result, 'trade_date')


def collect_index(provider, identifier, day, count=2000):
    spec = INDICES[identifier]
    target = min(day, reference_target(spec['market']))
    if spec['market']=='CN':
        validate_collection('CN',target,config())
    endpoint = 'stock_zh_index_daily' if spec['market'] == 'CN' else 'index_global_hist_sina'
    source_symbol = {'JP':'日经225指数','KR':'首尔综合指数'}.get(spec['market'],spec['symbol'])
    raw = provider.call(endpoint, symbol=source_symbol)
    quarantine = []
    rows = index_rows(raw, identifier, target.isoformat(), count, quarantine)
    meta = dict(instrument_id=identifier, **spec, kind='index', unit='point', adjustment='none',
                source='akshare/' + endpoint, timezone=str(ZONES[spec['market']]),
                coverage='validated observed sessions; historical continuity not certified',
                validation_report=dict(quarantined_rows=len(quarantine), rejected=quarantine))
    count = save('index:' + identifier, meta, rows)
    if rows[-1]['trade_date'] != target.isoformat():
        raise PendingData('target index date absent; dated history retained')
    return count


def index_report(identifier, start=None, end=None, limit=500):
    if identifier not in INDICES:
        raise HTTPException(404, 'unknown reference index')
    target = min(end or reference_target(INDICES[identifier]['market']), reference_target(INDICES[identifier]['market']))
    data = saved('index:' + identifier, start.isoformat() if start else None, target.isoformat(), limit)
    dates = [r['trade_date'] for r in data['items']]
    return dict(data, requested_end=target.isoformat(), latest_date=max(dates, default=None),
                stale=not dates or dates[-1] != target.isoformat())


def indices_report():
    items = []
    for identifier, spec in INDICES.items():
        try:
            payload = index_report(identifier, limit=2)
            last = payload['items'][-1] if payload['items'] else {}
            previous = payload['items'][-2] if len(payload['items']) == 2 else None
            # Compute return only across adjacent local sessions.
            change = None
            if previous and last:
                cursor = date.fromisoformat(last['trade_date']) - timedelta(days=1)
                while reference_close(spec['market'], cursor) is None:
                    cursor -= timedelta(days=1)
                if previous['trade_date'] == cursor.isoformat():
                    change = (last['close'] / previous['close'] - 1) * 100
            items.append(dict(id=identifier, **spec, **last, unit='point', source=payload['source'],
                              change_pct=change, stale=payload['stale'], status='available' if last else 'missing'))
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            items.append(dict(id=identifier, **spec, unit='point', status='not_collected', stale=True))
    return dict(items=items, coverage='configured reference indices; prices are index points')


def stock_id(identifier):
    if not re.fullmatch(r'CN\.stock\.\d{6}', identifier):
        raise HTTPException(422, 'CN stock ID required')
    with db.connect() as con:
        if not con.execute('SELECT 1 FROM instruments WHERE id=?', (identifier,)).fetchone():
            raise HTTPException(404, 'unknown stock')


def minute_rows(raw, target):
    result = []
    if session_close('CN', date.fromisoformat(target), config()) is None:
        raise ValueError('minute date is not a market session')
    for r in raw:
        stamp = datetime.fromisoformat(str(r['时间']))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=CN)
        if stamp.utcoffset() != timedelta(hours=8) or stamp.date().isoformat() != target:
            raise ValueError('minute date/timezone mismatch')
        clock = stamp.timetz().replace(tzinfo=None)
        if stamp.second or not (time(9, 30) <= clock <= time(11, 30) or time(13) <= clock <= time(15)):
            raise ValueError('minute timestamp outside supported session')
        prices = {k: numeric(r.get(v), required=True, minimum=0.0000001)
                  for k, v in [('close', '收盘'), ('high', '最高'), ('low', '最低')]}
        # Older 1-minute responses explicitly use 0 for unavailable opens.
        opening = numeric(r.get('开盘'), minimum=0)
        opening = opening if opening else None
        if not prices['low'] <= prices['close'] <= prices['high'] or (opening is not None and not prices['low'] <= opening <= prices['high']):
            raise ValueError('minute price bounds conflict')
        result.append(dict(timestamp=stamp.isoformat(), open=opening, **prices,
                           volume=numeric(r.get('成交量'), minimum=0), amount=numeric(r.get('成交额'), minimum=0),
                           average=numeric(r.get('均价'), minimum=0)))
    return unique_rows(result, 'timestamp')


def collect_minutes(provider, identifier, day):
    stock_id(identifier)
    raw = provider.call('stock_zh_a_hist_min_em', symbol=identifier.rsplit('.', 1)[-1], period='1', adjust='',
                        start_date=day.isoformat() + ' 09:30:00', end_date=day.isoformat() + ' 15:00:00')
    rows = minute_rows(raw, day.isoformat())
    return save('minutes:' + identifier + ':' + day.isoformat(),
                dict(instrument_id=identifier, trade_date=day.isoformat(), period='1m', adjustment='none',
                     timezone='Asia/Shanghai', currency='CNY', volume_unit='lot_100_shares', amount_unit='yuan',
                     source='akshare/stock_zh_a_hist_min_em', coverage='observed points; full-session completeness not certified',
                     historical_window='provider recent ~5 sessions; previously collected dates retained',
                     open_policy='provider zero open becomes null; prices are not reconstructed'), rows, 'timestamp', replace=True)


def minutes_report(identifier, day):
    stock_id(identifier)
    if day > latest_ready('CN', config()) or session_close('CN', day, config()) is None:
        raise HTTPException(422, 'completed CN market session required')
    return saved('minutes:' + identifier + ':' + day.isoformat(), limit=500)


def valuation_rows(raw, target):
    mapping = {'close': '当日收盘价', 'change_pct': '当日涨跌幅', 'total_market_cap': '总市值',
               'float_market_cap': '流通市值', 'total_shares': '总股本', 'float_shares': '流通股本',
               'pe_ttm': 'PE(TTM)', 'pe_static': 'PE(静)', 'pb': '市净率', 'peg': 'PEG值', 'pcf': '市现率', 'ps': '市销率'}
    result = []
    for r in raw:
        day = day_string(r['数据日期'])
        if day > target:
            continue
        if session_close('CN', date.fromisoformat(day), config()) is None:
            raise ValueError('valuation date is not a market session')
        point = dict(trade_date=day, **{k: numeric(r.get(v)) for k, v in mapping.items()})
        for name in ('close', 'total_market_cap', 'float_market_cap', 'total_shares', 'float_shares'):
            if point[name] is not None and point[name] <= 0:
                raise ValueError('nonpositive valuation price/capital')
        if point['float_shares'] and point['total_shares'] and point['float_shares'] > point['total_shares'] * 1.0001:
            raise ValueError('float shares exceed total shares')
        result.append(point)
    return unique_rows(result, 'trade_date')


def collect_valuation(provider, identifier, day):
    stock_id(identifier)
    raw = provider.call('stock_value_em', symbol=identifier.rsplit('.', 1)[-1])
    rows = valuation_rows(raw, day.isoformat())
    count = save('valuation:' + identifier,
                 dict(instrument_id=identifier, currency='CNY', amount_unit='yuan', shares_unit='share',
                      source='akshare/stock_value_em', pe_basis='provider TTM and static retained separately; negative ratios retained'), rows)
    if rows[-1]['trade_date'] != day.isoformat():
        raise PendingData('target valuation date absent; history retained')
    return count


def valuation_report(identifier, end=None, limit=1):
    stock_id(identifier)
    target = min(end or latest_ready('CN', config()), latest_ready('CN', config()))
    data = saved('valuation:' + identifier, end=target.isoformat(), limit=limit)
    with db.connect() as con:
        bars = {r['trade_date']: dict(r) for r in con.execute('SELECT * FROM bars WHERE instrument_id=? AND trade_date<=?', (identifier, target.isoformat()))}
    for r in data['items']:
        bar = bars.get(r['trade_date'])
        r.update(turnover_pct=None, turnover_basis=None)
        # Dated circulating shares and same-day verified share volume; never use a current snapshot for history.
        if (bar and bar['volume_unit'] == 'share' and bar['source'] in ('akshare/tencent', 'akshare/sina')
                and bar['volume'] is not None and r['float_shares'] and r['close']
                and abs(bar['close'] - r['close']) <= max(0.0001, r['close'] * 0.0001)):
            r['turnover_pct'] = bar['volume'] / r['float_shares'] * 100
            r['turnover_basis'] = dict(method='same_day_share_volume / dated_circulating_shares * 100',
                                       volume_source=bar['source'], shares_source=data['source'],
                                       denominator='circulating shares; not free-float shares')
    latest = max((r['trade_date'] for r in data['items']), default=None)
    return dict(data, requested_end=target.isoformat(), latest_date=latest, stale=latest != target.isoformat())


def pool_rows(raw, day):
    rows = []
    for r in raw:
        symbol = str(r['代码']).zfill(6)
        if not re.fullmatch(r'\d{6}', symbol):
            raise ValueError('invalid limit-up symbol')
        streak = numeric(r['连板数'], required=True, minimum=1)
        if streak != int(streak):
            raise ValueError('invalid reported streak')
        def clock(field):
            value = str(r[field]).replace(':', '').zfill(6)
            stamp = datetime.combine(day, time.fromisoformat(value), CN)
            if not time(9, 25) <= stamp.time() <= time(15):
                raise ValueError('invalid limit-up timestamp')
            return stamp.isoformat()
        rows.append(dict(symbol=symbol, instrument_id='CN.stock.' + symbol, name=str(r['名称']),
                         trade_date=day.isoformat(), consecutive_days=int(streak),
                         close=numeric(r['最新价'], required=True, minimum=0.0000001),
                         change_pct=numeric(r['涨跌幅']), turnover_pct=numeric(r.get('换手率'), minimum=0),
                         amount=numeric(r.get('成交额'), minimum=0), float_market_cap=numeric(r.get('流通市值'), minimum=0),
                         total_market_cap=numeric(r.get('总市值'), minimum=0), seal_amount=numeric(r.get('封板资金'), minimum=0),
                         first_seal_at=clock('首次封板时间'), last_seal_at=clock('最后封板时间'),
                         broken_count=numeric(r.get('炸板次数'), minimum=0),
                         reported_stat=r.get('涨停统计'), reported_industry=r.get('所属行业')))
    return unique_rows(rows, 'symbol')


def collect_pool(provider, day):
    raw = provider.call('stock_zt_pool_em', date=day.strftime('%Y%m%d'))
    rows = pool_rows(raw, day)
    return save('limit-up:' + day.isoformat(),
                dict(trade_date=day.isoformat(), currency='CNY', amount_unit='yuan', source='akshare/stock_zt_pool_em',
                     basis='provider_reported', independently_verified=False,
                     coverage='provider recent limit-up pool; not certified whole-market/ST/IPO coverage',
                     classification_source='eastmoney; no THS membership implied'), rows, 'symbol', replace=True)


def pool_report(day):
    if day > latest_ready('CN', config()) or session_close('CN', day, config()) is None:
        raise HTTPException(422, 'completed CN market session required')
    payload = saved('limit-up:' + day.isoformat(), limit=5000)
    payload['items'].sort(key=lambda r: (-r['consecutive_days'], r['symbol']))
    groups = {}
    for row in payload['items']:
        groups.setdefault(row['consecutive_days'], []).append(row['symbol'])
    return dict(payload, total=len(payload['items']), groups=[dict(consecutive_days=k, count=len(v), symbols=v)
                for k, v in sorted(groups.items(), reverse=True)])


def readiness():
    with db.connect() as con:
        return [dict(r) for r in con.execute('''SELECT f.feed_key,f.verified_at,count(p.point_key) AS stored_rows,
            min(p.point_key) AS first_key,max(p.point_key) AS last_key FROM supplemental_feeds f
            LEFT JOIN supplemental_points p ON p.feed_key=f.feed_key GROUP BY f.feed_key ORDER BY f.feed_key''')]


def run(cfg, day, features, symbols=None, output=None):
    """One explicit finite collection, with per-source attempts and optional raw evidence."""
    from pathlib import Path
    from .provider import AKProvider
    from .pipeline import CollectorLock
    # Foreign reference indices have their own sessions, including China holidays.
    if set(features)-{'indices'}:
        validate_collection('CN', day, cfg)
    base = dict(cfg, request_attempts=1, request_timeout_seconds=35, request_spacing_seconds=1)
    provider = AKProvider(base)
    curl = AKProvider(dict(base, akshare_transport='curl_cffi'))
    output = Path(output) if output else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    class Recording:
        def __init__(self, delegate): self.delegate = delegate
        def call(self, endpoint, **kwargs):
            rows = self.delegate.call(endpoint, **kwargs)
            if output:
                name = endpoint + '-' + re.sub(r'[^a-zA-Z0-9_-]', '_', str(kwargs.get('symbol', kwargs.get('date', 'all'))))
                (output / (name + '.json')).write_text(json.dumps(dict(endpoint=endpoint, kwargs=kwargs, rows=rows), ensure_ascii=False, allow_nan=False), encoding='utf-8')
            return rows
    direct, enhanced = Recording(provider), Recording(curl)
    jobs = []
    if 'indices' in features:
        jobs += [('index:' + i, lambda i=i: collect_index(direct if INDICES[i]['market']=='CN' else enhanced, i, day)) for i in INDICES]
    symbols = symbols or cfg['cn_stocks']
    for symbol in symbols:
        identifier = 'CN.stock.' + symbol
        if 'minutes' in features:
            jobs.append(('minutes:' + identifier + ':' + day.isoformat(), lambda i=identifier: collect_minutes(enhanced, i, day)))
        if 'valuation' in features:
            jobs.append(('valuation:' + identifier, lambda i=identifier: collect_valuation(enhanced, i, day)))
    if 'limit-up' in features:
        jobs.append(('limit-up:' + day.isoformat(), lambda: collect_pool(direct, day)))
    results = []
    with CollectorLock():
        for key, action in jobs:
            try:
                if not key.startswith('index:'):
                    validate_collection('CN', day, cfg)
                count = action()
                db.feed_attempt(key, 'complete')
                result = dict(feed_key=key, status='complete', rows=count)
            except Exception as exc:
                state = 'pending' if isinstance(exc, PendingData) else 'failed'
                db.feed_attempt(key, state, type(exc).__name__)
                result = dict(feed_key=key, status=state, error=type(exc).__name__)
            results.append(result)
            print(json.dumps(result), flush=True)
    return dict(trade_date=day.isoformat(), status='complete' if all(r['status'] == 'complete' for r in results) else 'partial',
                items=results, scope='explicit configured collection; automatic jobs remain unchanged')
