"""The desktop watchlist's bounded, cached AKShare snapshot."""
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Event, Thread

from fastapi import HTTPException

from . import db, normalize, public_feeds
from .calendars import CN, session_close
from .pipeline import CollectorLock
from .provider import AKProvider
from .quotes import parse_tencent
from .settings import config


log = logging.getLogger(__name__)
INTERVAL_SECONDS = 15 * 60
SCHEDULED_MINUTES = (
    frozenset(range(9 * 60 + 26, 11 * 60 + 30, INTERVAL_SECONDS // 60))
    | frozenset(range(13 * 60 + 1, 15 * 60, INTERVAL_SECONDS // 60))
)
REQUEST_TIMEOUT_SECONDS = 60
AKSHARE_COOLDOWN_SECONDS = 60 * 60
FEED_KEY = 'looklook:quotes'
SCHEDULE_KEY = FEED_KEY + ':schedule'
AKSHARE_KEY = FEED_KEY + ':akshare'
TENCENT_KEY = FEED_KEY + ':tencent'
ENDPOINT = 'stock_zh_a_spot_em'
SOURCE = 'akshare/' + ENDPOINT
TENCENT_SOURCE = 'tencent/quote'
TENCENT_BATCH_SIZE = 50
WATCHLIST = Path(__file__).resolve().parents[1] / 'config' / 'looklook-watchlist.json'


@lru_cache
def symbols():
    watchlist = json.loads(WATCHLIST.read_text(encoding='utf-8'))
    codes = frozenset(stock['code'] for group in watchlist['groups'] for stock in group['stocks'])
    if not codes or any(not re.fullmatch(r'(?:00[0-3]|30[01]|60[0135])\d{3}', code) for code in codes):
        raise ValueError('looklook watchlist requires six-digit main-board or ChiNext codes')
    return codes


def select_quotes(rows, selected):
    quotes = {}
    seen = set()
    for row in rows:
        code = str(row.get('代码', '')).zfill(6)
        if code not in selected:
            continue
        if code in seen:
            raise ValueError('duplicate watchlist quote symbol')
        seen.add(code)
        try:
            change = normalize.number(row.get('涨跌幅'))
            price = normalize.number(row.get('最新价'))
            volume = normalize.number(row.get('成交量'))
        except (ValueError, TypeError, OverflowError):
            continue
        if change is not None and price is not None and price > 0 and volume is not None and volume > 0:
            quotes[code] = {'changePct': change}
    return quotes


def tencent_quotes(selected, source):
    codes = [('sh' if code.startswith('6') else 'sz') + code for code in sorted(selected)]
    by_date = {}
    for offset in range(0, len(codes), TENCENT_BATCH_SIZE):
        batch = codes[offset:offset + TENCENT_BATCH_SIZE]
        try:
            text = source.get('https://qt.gtimg.cn/q=' + ','.join(batch), encoding='gb18030', timeout=(5, 8))
            records = parse_tencent(text, batch)
        except Exception as exc:
            log.warning('looklook Tencent batch failed: %s', type(exc).__name__)
            continue
        received = datetime.now(timezone.utc)
        for record in records:
            stamp = datetime.fromisoformat(record['source_time'])
            if stamp > received or record['change_pct'] is None:
                continue
            trade_date = stamp.astimezone(CN).date().isoformat()
            by_date.setdefault(trade_date, {})[record['symbol']] = {
                'changePct': record['change_pct'], 'quotedAt': record['source_time'],
            }
    if not by_date:
        raise ValueError('Tencent returned no usable looklook quotes')
    trade_date = max(by_date)
    return by_date[trade_date], trade_date


def _load(key, now):
    try:
        return public_feeds.load(key, now=now)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        return None


def _failed_recently(key, now):
    with db.connect() as con:
        last = con.execute("SELECT last_attempt FROM feed_attempts WHERE feed_key=? AND status='failed'",
                           (key,)).fetchone()
    return last and (now - datetime.fromisoformat(last[0])).total_seconds() < AKSHARE_COOLDOWN_SECONDS


def refresh(cfg, *, provider=None, tencent=None):
    with CollectorLock('looklook-quotes.lock'):
        selected = symbols()
        quotes = {}
        now = datetime.now(timezone.utc)
        if not _failed_recently(AKSHARE_KEY, now):
            if provider is None:
                provider = AKProvider(dict(cfg, request_attempts=1, request_spacing_seconds=0,
                                           request_timeout_seconds=REQUEST_TIMEOUT_SECONDS, raw_response_dir=None))
            try:
                quotes = select_quotes(provider.call(ENDPOINT), selected)
                if not quotes:
                    raise ValueError('AKShare returned no usable looklook quotes')
            except Exception as exc:
                reason = type(exc).__name__
                db.feed_attempt(AKSHARE_KEY, 'failed', reason)
                log.warning('looklook AKShare unavailable for this cycle: %s', reason)

        if quotes:
            # AKShare reports neither an exchange timestamp nor a trading date.
            key, source, trade_date = AKSHARE_KEY, SOURCE, None
        else:
            key, source = TENCENT_KEY, TENCENT_SOURCE
            try:
                quotes, trade_date = tencent_quotes(selected, tencent or public_feeds.PublicSource())
                previous = _load(key, datetime.now(timezone.utc))
                if previous and trade_date < previous['trade_date']:
                    raise ValueError('Tencent trade date moved backwards')
            except Exception as exc:
                reason = type(exc).__name__
                db.feed_attempt(TENCENT_KEY, 'failed', reason)
                db.feed_attempt(FEED_KEY, 'failed', reason)
                log.warning('looklook refresh failed: %s', reason)
                return {'status': 'failed', 'reason': reason}

        public_feeds.store(key, {'source': source, 'items': quotes, 'trade_date': trade_date})
        db.feed_attempt(FEED_KEY, 'complete')
        status = 'complete' if len(quotes) == len(selected) else 'partial'
        log.info('looklook refresh %s via %s: %d/%d quotes', status, source, len(quotes), len(selected))
        return {'status': status, 'source': source, 'tradeDate': trade_date,
                'count': len(quotes), 'expected': len(selected)}


def snapshot(*, now=None):
    now = now or datetime.now(timezone.utc)
    selected = symbols()
    snapshots = [value for key in (AKSHARE_KEY, TENCENT_KEY) if (value := _load(key, now))]
    if not snapshots:
        return {'updatedAt': None, 'tradeDate': None, 'source': None, 'stale': True,
                'partial': True, 'quotes': {}}

    stored = max(snapshots, key=lambda value: value['collected_at'])
    stamp = datetime.fromisoformat(stored['collected_at'])
    quotes = {code: quote for code, quote in stored['items'].items() if code in selected}
    with db.connect() as con:
        last = con.execute("SELECT last_attempt FROM feed_attempts WHERE feed_key=? AND status='failed'",
                           (FEED_KEY,)).fetchone()
    failed = bool(last and last[0] >= stored['collected_at'])
    stale = failed or not quotes or (now - stamp).total_seconds() > INTERVAL_SECONDS + REQUEST_TIMEOUT_SECONDS
    return {'updatedAt': stored['collected_at'], 'tradeDate': stored['trade_date'],
            'source': stored['source'], 'stale': stale, 'partial': len(quotes) < len(selected), 'quotes': quotes}


def collect_scheduled_slot(cfg, now):
    """Claim and run the current Shanghai-time slot, if one is due."""
    if not cfg.get('automatic_collection', False) or not cfg.get('looklook_quotes_enabled', True):
        return None

    local = now.astimezone(CN)
    if local.hour * 60 + local.minute not in SCHEDULED_MINUTES:
        return None
    close = session_close('CN', local.date(), cfg)
    if close is None or local >= close:
        return None

    # A single durable claim prevents repeat attempts after process restarts.
    slot = local.replace(second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    with db.connect() as con:
        claimed = con.execute('''
            INSERT INTO feed_attempts (feed_key, last_attempt, attempts, status)
            VALUES (?, ?, 1, 'claimed')
            ON CONFLICT(feed_key) DO UPDATE SET
                last_attempt=excluded.last_attempt, attempts=feed_attempts.attempts+1
            WHERE feed_attempts.last_attempt < excluded.last_attempt
        ''', (SCHEDULE_KEY, slot)).rowcount
    if claimed:
        return refresh(cfg)
    return None


@contextmanager
def worker_clock():
    stop = Event()

    def poll():
        while not stop.is_set():
            try:
                collect_scheduled_slot(config(), datetime.now(CN))
            except Exception as exc:
                log.warning('looklook collection unavailable: %s', type(exc).__name__)
            now = datetime.now(CN)
            stop.wait(60 - now.second - now.microsecond / 1_000_000)

    thread = Thread(target=poll, name='looklook-quotes', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
