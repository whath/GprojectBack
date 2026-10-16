"""A small read-only manifest for a UI driven by available stored data."""
import json
from datetime import datetime, timezone

from . import db
from .calendars import CN, latest_ready


def report(cfg):
    targets={m:latest_ready(m,cfg).isoformat() for m in ('CN','US')}
    with db.connect() as con:
        dates=dict(con.execute("SELECT i.market,max(b.trade_date) FROM bars b JOIN instruments i ON i.id=b.instrument_id WHERE b.adjustment='none' GROUP BY i.market"))
        board_date=con.execute("SELECT max(b.trade_date) FROM bars b JOIN instruments i ON i.id=b.instrument_id WHERE i.market='CN' AND i.kind IN ('industry','concept') AND i.symbol LIKE 'BK%'").fetchone()[0]
        lhb_date=con.execute("SELECT max(trade_date) FROM datasets WHERE dataset='lhb'").fetchone()[0]
        member_date=con.execute("SELECT max(trade_date) FROM datasets WHERE dataset LIKE 'members:CN.%BK%'").fetchone()[0]
        feeds=[r[0] for r in con.execute("SELECT feed_key FROM feed_snapshots WHERE status='complete'")]
        supplemental=[r[0] for r in con.execute('SELECT feed_key FROM supplemental_feeds')]
        count=con.execute("SELECT count(DISTINCT i.id) FROM instruments i JOIN bars b ON b.instrument_id=i.id WHERE i.market='CN' AND i.kind='stock'").fetchone()[0]
    return dict(api_version='0.6.1',history_sessions=30,stock_history_weeks=cfg.get('stock_history_weeks',0),generated_at=db.now_iso(),
                calendar_targets=targets,latest_dates=dict(cn=dates.get('CN'),us=dates.get('US'),boards=board_date,
                    lhb=lhb_date,membership_observed=member_date),
                cn_stocks_with_history=count,
                detail_features=dict(funds=[k.removeprefix('flows:') for k in feeds if k.startswith('flows:CN.stock.')],
                    metrics=[k.removeprefix('valuation:') for k in supplemental if k.startswith('valuation:')],
                    minutes=[k.removeprefix('minutes:') for k in supplemental if k.startswith('minutes:')]),
                features=dict(gainers=any(k.startswith('adjusted:') for k in feeds),boards=bool(board_date),
                    board_funds=any(k.startswith('board-flows:') for k in feeds),macro=any(k.startswith('macro:') for k in feeds),
                    calendar='calendar:wscn' in feeds,policy=any(k.startswith('policy:') for k in feeds),lhb=bool(lhb_date)),
                removed_features=['limit_up_ladder','model_inference','long_history_expansion','ths_board_funds'],
                automatic_collection_enabled=bool(cfg.get('automatic_collection',False)))
