import json
from datetime import date,timedelta

from fastapi.testclient import TestClient

from marketdata import db,normalize
from marketdata.api import app
from marketdata.gainers import ranking
from marketdata.history import sessions,week_sessions
from marketdata.pipeline import Collector
from marketdata.public_feeds import store
from marketdata.settings import config

DAY=date(2026,10,15)
HEADERS={'Authorization':'Bearer test-secret-token-that-is-at-least-32-characters'}


def seed():
    item=Collector('CN',DAY).item('stock','000001','样本','sz000001')
    db.instrument(item)
    days=week_sessions('CN',DAY,52,config())
    rows=normalize.bars([{'日期':d,'开盘':10,'最高':11,'最低':9,'收盘':10,'成交量':100,'volume_unit':'share'} for d in days],str(DAY),'stock','CN')
    db.put_bars(item['id'],rows)
    return item,days


def test_52_calendar_weeks_follow_market_calendar_not_260_sessions():
    days=week_sessions('CN',DAY,52,config())
    first=DAY-timedelta(weeks=52)+timedelta(days=1)
    assert days[0]>=str(first) and days[-1]==str(DAY)
    assert 200<len(days)<260 and days==sorted(set(days))


def test_explicit_52_week_query_and_legacy_30_day_query(monkeypatch):
    item,days=seed()
    monkeypatch.setattr('marketdata.api.config',lambda:dict(config(),stock_history_weeks=52))
    with TestClient(app) as client:
        extended=client.get('/v1/bars/'+item['id']+'?end=2026-10-15&weeks=52&limit=400',headers=HEADERS)
        assert extended.status_code==200
        body=extended.json()
        assert [r['trade_date'] for r in body['items']]==days
        assert body['window_weeks']==52 and body['window_sessions']==len(days)
        assert body['history_coverage']['complete']
        assert len(client.get('/v1/bars/'+item['id']+'?end=2026-10-15&weeks=52',headers=HEADERS).json()['items'])==len(days)
        checked=client.get('/v1/history-coverage/'+item['id']+'?end=2026-10-15&weeks=52',headers=HEADERS).json()
        assert checked['window']==len(days) and checked['complete'] and checked['window_weeks']==52
        legacy=client.get('/v1/bars/'+item['id']+'?end=2026-10-15&limit=2000',headers=HEADERS).json()
        assert len(legacy['items'])==legacy['window_sessions']==30
        assert not legacy['collection_on_read']
        assert client.get('/v1/bars/'+item['id']+'?weeks=53',headers=HEADERS).status_code==422
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM history_requests').fetchone()[0]==0


def test_unpublished_52_week_data_is_not_advertised_as_available():
    item,_=seed()
    with TestClient(app) as client:
        assert client.get('/v1/bars/'+item['id']+'?weeks=52',headers=HEADERS).status_code==422


def test_six_session_rank_is_unchanged_by_52_week_snapshot():
    item,days=seed()
    recent=sessions('CN',DAY,6,config())
    points=[{'trade_date':d,'close':10+i} for i,d in enumerate(days)]
    payload=dict(source='akshare/stock_zh_a_hist_tx/qfq/v1',adjustment='qfq',target_date=str(DAY))
    store('adjusted:'+item['id'],dict(payload,items=points))
    whole=ranking(DAY,config())
    store('adjusted:'+item['id'],dict(payload,items=[p for p in points if p['trade_date'] in recent]))
    short=ranking(DAY,config())
    assert whole==short and whole['eligible_count']==1


def test_stock_with_older_year_history_remains_searchable_after_long_halt(monkeypatch):
    item,days=seed()
    with db.connect() as con:
        con.execute('DELETE FROM bars WHERE instrument_id=? AND trade_date>?',(item['id'],days[20]))
    monkeypatch.setattr('marketdata.api.config',lambda:dict(config(),stock_history_weeks=52))
    with TestClient(app) as client:
        response=client.get('/v1/instruments?market=CN&kind=stock&available_only=true&search=000001',headers=HEADERS)
        assert response.status_code==200 and response.json()['items'][0]['id']==item['id']
