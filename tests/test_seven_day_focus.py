from datetime import date

from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.gainers import ranking
from marketdata.history import sessions
from marketdata.public_feeds import store
from marketdata.pipeline import Collector
from marketdata.settings import config

DAY = date(2026, 10, 15)
HEADERS = {'Authorization': 'Bearer test-secret-token-that-is-at-least-32-characters'}


def seed(symbol, closes, missing=()):
    item = Collector('CN',DAY).item('stock',symbol,symbol,'sz'+symbol)
    db.instrument(item)
    days = sessions('CN',DAY,8,config())
    bars = normalize.bars([dict(日期=d,开盘=20,最高=21,最低=19,收盘=20,成交量=100) for d in days],str(DAY),'stock','CN')
    db.put_bars(item['id'],bars)
    store('adjusted:'+item['id'],dict(instrument_id=item['id'],source='test/same-fetch/qfq',adjustment='qfq',
        target_date=str(DAY),items=[dict(trade_date=d,close=c) for index,(d,c) in enumerate(zip(days,closes)) if index not in missing]))
    return item


def test_seven_sessions_need_eight_exact_closes_and_raw_quote():
    first=seed('000001',[10,11,12,13,14,15,16,17])
    seed('000002',[10,11,12,13,14,15,16,18],missing=(0,))
    result=ranking(DAY,config(),period=7)
    assert result['period']=='7d' and result['period_sessions']==7
    assert len(result['required_sessions'])==8
    assert result['eligible_count']==1 and result['missing_data_count']==1
    row=result['items'][0]
    assert row['id']==first['id'] and abs(row['change_7d_pct']-70)<1e-10
    assert row['close']==20 and row['adjusted_close']==17
    assert 'change_5d_pct' not in row
    # The missing oldest close must not be silently treated as a shorter window.
    assert ranking(DAY,config(),period=5)['eligible_count']==2


def test_top_fifty_keeps_whole_universe_coverage_and_snapshot_period_identity():
    for index in range(55):
        seed(f'{index+1:06d}',[10,10,10,10,10,10,10,11+index])
    with TestClient(app) as client:
        url='/v1/cn/rankings?kind=stock&period=7d&trade_date=2026-10-15&top=50&limit=50'
        reply=client.get(url,headers=HEADERS)
        assert reply.status_code==200
        body=reply.json()
        assert len(body['items'])==body['total']==50
        assert body['eligible_count']==body['expected_count']==55
        assert body['items'][0]['symbol']=='000055' and body['items'][-1]['rank']==50
        # A five-day snapshot cannot be reused to serve a seven-day request.
        wrong=client.get(url.replace('period=7d','period=5d')+'&snapshot_id='+body['snapshot_id'],headers=HEADERS)
        assert wrong.status_code in (404,409)
