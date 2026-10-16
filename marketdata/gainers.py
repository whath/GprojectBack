"""Five exact session returns. Adjusted stock/ETF snapshots never mix vintages."""
from datetime import date

from . import db, normalize
from .calendars import latest_ready, validate_collection
from .history import sessions
from .public_feeds import store


def collect(cfg, target, max_items=100, scope='configured'):
    from .provider import AKProvider
    validate_collection('CN',target,cfg)
    allowed={'CN.'+kind+'.'+symbol for kind,field in [('stock','cn_stocks'),('etf','cn_etfs')] for symbol in cfg[field]}
    with db.connect() as con:
        items=[dict(r) for r in con.execute("SELECT * FROM instruments WHERE market='CN' AND kind IN ('stock','etf') ORDER BY id")]
        states={r['feed_key']:r['last_attempt'] for r in con.execute("SELECT * FROM feed_attempts WHERE feed_key LIKE 'adjusted:%'")}
        available={r[0] for r in con.execute("SELECT DISTINCT instrument_id FROM bars WHERE trade_date=? AND adjustment='none'",(str(target),))}
    items=[r for r in items if scope=='catalog' or (r['id'] in available if scope=='available' else r['id'] in allowed)]
    items.sort(key=lambda r:(states.get('adjusted:'+r['id'],''),r['id']))
    provider=AKProvider(dict(cfg,request_attempts=1,request_timeout_seconds=35))
    expected=sessions('CN',target,30,cfg)
    results=[]
    for item in items[:max_items]:
        key='adjusted:'+item['id']
        try:
            symbol=item['symbol']
            if item['kind']=='etf':
                endpoint='fund_etf_hist_em'
                kwargs=dict(symbol=symbol,period='daily')
            elif symbol.startswith(('0','3','6')):
                endpoint='stock_zh_a_hist_tx'
                kwargs=dict(symbol=('sh' if symbol.startswith('6') else 'sz')+symbol,timeout=15)
            else:
                endpoint='stock_zh_a_daily'
                kwargs=dict(symbol='bj'+symbol)
            kwargs.update(start_date=expected[0].replace('-',''),end_date=str(target).replace('-',''),adjust='qfq')
            raw=provider.call(endpoint,**kwargs)
            rows=normalize.bars([r for r in raw if str(r['日期'])[:10] in expected],str(target),item['kind'],'CN')
            if not rows or rows[-1]['trade_date']!=str(target):
                raise normalize.PendingData('adjusted target close absent')
            # Entire snapshot is replaced: front adjustment can revise every prior value.
            store(key,dict(instrument_id=item['id'],source='akshare/'+endpoint+'/qfq/v1',adjustment='qfq',
                           basis='same-provider front-adjusted closes from one fetch',target_date=str(target),window=30,
                           items=[dict(trade_date=r['trade_date'],close=r['close']) for r in rows]))
            results.append(dict(feature=key,status='complete',rows=len(rows)))
        except Exception as exc:
            db.feed_attempt(key,'failed',type(exc).__name__)
            results.append(dict(feature=key,status='pending' if isinstance(exc,normalize.PendingData) else 'failed',error=type(exc).__name__))
    return results


def ranking(target, cfg, kind='stock', classification='eastmoney', connection=None):
    from fastapi import HTTPException
    from .calendars import session_close
    if target>latest_ready('CN',cfg) or session_close('CN',target,cfg) is None:
        raise HTTPException(422,'completed CN market session required')
    import json
    from contextlib import nullcontext
    from .macro_assets import finite
    expected_days=sessions('CN',target,6,cfg)
    board=kind in ('industry','concept')
    clauses="i.market='CN' AND i.kind=? AND COALESCE(l.status,'listed')='listed'"
    params=[kind]
    if board:
        clauses+=' AND i.symbol LIKE ?'
        params.append('BK%' if classification=='eastmoney' else 'THS_%')
    with nullcontext(connection) if connection is not None else db.connect() as con:
        instruments=[dict(r) for r in con.execute('SELECT i.* FROM instruments i LEFT JOIN instrument_lifecycle l ON l.instrument_id=i.id WHERE '+clauses+' ORDER BY i.id',params)]
        bars={}
        if board:
            for r in con.execute("SELECT * FROM bars WHERE trade_date>=? AND trade_date<=? AND adjustment='none'",(expected_days[0],str(target))):
                bars.setdefault(r['instrument_id'],[]).append(dict(r))
        feeds={r['feed_key']:json.loads(r['payload']) for r in con.execute("SELECT * FROM feed_snapshots WHERE feed_key LIKE 'adjusted:%' AND status='complete'")}
        market_closes={r['instrument_id']:dict(r) for r in con.execute("SELECT instrument_id,close,source FROM bars WHERE trade_date=? AND adjustment='none'",(str(target),))}
    rows,excluded=[],{}
    for item in instruments:
        feed=feeds.get('adjusted:'+item['id']) if not board else None
        points=bars.get(item['id'],[]) if board else (feed or {}).get('items',[])
        selected=[r for r in points if r['trade_date'] in expected_days]
        reason=None
        if not selected: reason='not_collected'
        elif len(selected)!=6 or {r['trade_date'] for r in selected}!=set(expected_days):reason='missing_market_sessions'
        elif board and len({r['source'] for r in selected})!=1:reason='mixed_source'
        elif not board and (feed.get('adjustment')!='qfq' or not feed.get('source')):reason='unqualified_adjustment'
        elif not board and item['id'] not in market_closes:reason='no_viewable_daily_bar'
        if reason:
            excluded[reason]=excluded.get(reason,0)+1
            continue
        selected.sort(key=lambda r:r['trade_date'])
        prices=[finite(r['close']) for r in selected]
        if min(prices)<=0:
            excluded['invalid_close']=excluded.get('invalid_close',0)+1
            continue
        change=(prices[-1]/prices[0]-1)*100
        # Quote prices are raw market closes. Adjusted closes are for return
        # calculation and can be restated by a later corporate action.
        quote=market_closes.get(item['id'])
        rows.append(dict(id=item['id'],name=item['name'],symbol=item['symbol'],close=prices[-1] if board else quote['close'],
                         adjusted_close=prices[-1] if not board else None,close_basis='board_index' if board else 'unadjusted_market_close',
                         price_source=selected[-1]['source'] if board else quote['source'],
                         trade_date=str(target),price_change_pct=change,change_5d_pct=change,
                         source=selected[-1]['source'] if board else feed['source'],
                         adjustment='none' if board else 'qfq',baseline_date=expected_days[0]))
    rows.sort(key=lambda r:(-r['change_5d_pct'],r['id']))
    for rank,row in enumerate(rows,1):row['rank']=rank
    return dict(items=rows,total=len(rows),trade_date=str(target),period='5d',period_sessions=5,
                baseline_date=expected_days[0],required_sessions=expected_days,expected_count=len(instruments),
                eligible_count=len(rows),excluded_count=len(instruments)-len(rows),exclusions=excluded,
                complete=bool(instruments) and len(rows)==len(instruments),scope='collected_qualified_instruments',
                classification_source=classification if board else None,
                ranking_metric='change_5d_pct',return_basis='board_index_close' if board else 'same-provider_qfq_close')
