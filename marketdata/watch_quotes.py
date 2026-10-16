"""Independent, bounded watch quotes from stored bars. GET never pulls a provider."""
import math
import re
from datetime import date

from fastapi import HTTPException

from . import db
from .calendars import latest_ready
from .history import sessions

IDENTIFIER = re.compile(r'(?:CN\.(?:stock|etf)\.\d{6}|CN\.(?:industry|concept)\.(?:BK\d{4}|THS_\d{6})|US\.(?:stock|etf)\.[A-Za-z0-9][A-Za-z0-9.\-]{0,19})')


def report(identifiers, cfg, cn_date=None, us_date=None):
    if not 1 <= len(identifiers) <= 100 or len(set(identifiers)) != len(identifiers):
        raise HTTPException(422, 'provide 1..100 unique instrument IDs')
    if any(not IDENTIFIER.fullmatch(i) for i in identifiers):
        raise HTTPException(422, 'invalid instrument ID')
    targets = {m: (d or latest_ready(m, cfg)).isoformat() for m, d in [('CN', cn_date), ('US', us_date)]}
    ready = {m: latest_ready(m, cfg).isoformat() for m in targets}
    if any(targets[m] > ready[m] for m in targets):
        raise HTTPException(422, 'requested date is not yet a completed session')
    previous = {m: sessions(m, date.fromisoformat(d), 2, cfg)[0] for m, d in targets.items()}
    result = []
    with db.connect() as con:
        # One read snapshot also keeps the quote and its prior close consistent.
        con.execute('BEGIN')
        for identifier in identifiers:
            market = identifier.split('.')[0]
            item = con.execute('SELECT * FROM instruments WHERE id=?', (identifier,)).fetchone()
            base = dict(id=identifier, requested_date=targets[market], price_type='stored_daily_close', adjustment='none')
            if not item:
                result.append(dict(base, status='not_found', close=None, change_1d_pct=None))
                continue
            base.update(name=item['name'], symbol=item['symbol'], currency=item['currency'], kind=item['kind'])
            bar = con.execute("SELECT * FROM bars WHERE instrument_id=? AND adjustment='none' AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
                              (identifier, targets[market])).fetchone()
            if not bar or not math.isfinite(bar['close']) or bar['close'] <= 0:
                result.append(dict(base, status='unavailable', close=None, change_1d_pct=None))
                continue
            change, basis = None, None
            if bar['trade_date'] == targets[market]:
                if bar['change_pct'] is not None and math.isfinite(bar['change_pct']):
                    change, basis = bar['change_pct'], 'provider_daily_change_pct'
                else:
                    prior = con.execute("SELECT close,source FROM bars WHERE instrument_id=? AND adjustment='none' AND trade_date=?",
                                        (identifier, previous[market])).fetchone()
                    if prior and prior['close'] > 0 and prior['source'] == bar['source']:
                        change = (bar['close'] / prior['close'] - 1) * 100
                        basis = 'unadjusted_close_vs_previous_market_session; corporate actions can distort'
            result.append(dict(base, status='current' if bar['trade_date'] == targets[market] else 'stale',
                               close=bar['close'], trade_date=bar['trade_date'], source=bar['source'],
                               collected_at=bar['collected_at'], change_1d_pct=change, change_basis=basis))
    return dict(items=result, total=len(result), as_of=targets, generated_at=db.now_iso(),
                note='Stored closes, not real-time quotes. Stale prices retain their actual dates; missing prices are null.')
