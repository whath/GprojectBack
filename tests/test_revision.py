import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.calendars import CN, validate_collection
from marketdata.calendar_feed import normalize as calendar_rows
from marketdata.board_data import flow_rows
from marketdata.gainers import ranking
from marketdata.history import sessions
from marketdata.macro_assets import brent_rows, gold_rows, treasury_rows
from marketdata.pipeline import Collector
from marketdata.policy_news import parse_listing
from marketdata.public_feeds import store, load
from marketdata.settings import config

HEADERS={'Authorization':'Bearer test-secret-token-that-is-at-least-32-characters'}
DAY=date(2026,10,15)


def seed(symbol='000001',count=40):
    item=Collector('CN',DAY).item('stock',symbol,'样本','sz'+symbol)
    db.instrument(item)
    days=sessions('CN',DAY,count,config())
    rows=normalize.bars([{'日期':d,'开盘':10,'最高':11,'最低':9,'收盘':10,'成交量':100,'volume_unit':'share'} for d in days],str(DAY),'stock','CN')
    db.put_bars(item['id'],rows)
    return item


def test_history_caps_to_market_window_without_expansion_queue():
    item=seed()
    with TestClient(app) as client:
        result=client.get('/v1/bars/'+item['id']+'?end=2026-10-15&limit=2000',headers=HEADERS)
        assert result.status_code==200
        body=result.json()
        assert len(body['items'])==30
        assert [r['trade_date'] for r in body['items']]==sessions('CN',DAY,30,config())
        assert body['history_coverage']['window']==30
    with db.connect() as con:
        assert con.execute('SELECT count(*) FROM history_requests').fetchone()[0]==0
        assert con.execute('SELECT count(*) FROM bars').fetchone()[0]==40


def test_past_session_manual_read_does_not_wait_for_todays_collection_slot():
    validate_collection('CN',DAY,config(),datetime(2026,10,16,10,tzinfo=CN))
    with pytest.raises(ValueError):
        validate_collection('CN',date(2026,10,16),config(),datetime(2026,10,16,10,tzinfo=CN))


def test_explicit_refresh_rechecks_whole_thirty_session_window():
    item=seed()
    starts=[]
    class Provider:
        def call(self,endpoint,**kwargs):
            starts.append(kwargs['start_date'])
            return [{'日期':str(DAY),'开盘':10,'最高':11,'最低':9,'收盘':10,'成交量':100,'volume_unit':'share'}]
    Collector('CN',DAY,provider=Provider()).history(item,'stock_zh_a_hist')
    Collector('CN',DAY,provider=Provider(),refresh=True).history(item,'stock_zh_a_hist')
    assert starts[0]>starts[1] and starts[1]==sessions('CN',DAY,30,config())[0].replace('-','')


def test_gainers_require_adjusted_snapshot_and_all_six_exact_sessions():
    item=seed()
    missing=seed('000002')
    empty=ranking(DAY,config())
    assert empty['eligible_count']==0  # Unadjusted bars are never silently used.
    days=sessions('CN',DAY,6,config())
    payload=dict(source='akshare/stock_zh_a_hist_tx/qfq/v1',adjustment='qfq',target_date=str(DAY),
                 items=[dict(trade_date=d,close=10+i) for i,d in enumerate(days)])
    store('adjusted:'+item['id'],dict(payload,instrument_id=item['id']))
    store('adjusted:'+missing['id'],dict(payload,instrument_id=missing['id'],items=payload['items'][:2]+payload['items'][3:]))
    result=ranking(DAY,config())
    assert result['eligible_count']==1 and result['expected_count']==2
    assert result['items'][0]['change_5d_pct']==50
    assert result['items'][0]['close']==10 and result['items'][0]['adjusted_close']==15
    assert result['exclusions']=={'missing_market_sessions':1}
    assert result['baseline_date']==days[0]


def test_fund_identity_and_main_order_definition_are_verified():
    line='2026-10-15,30,0,0,10,20,0,0,0,0,0,100,0,0,0'
    payload=dict(rc=0,data=dict(code='BK1034',market=90,klines=[line]))
    assert flow_rows(payload,'BK1034',DAY,config())[0]['net']==30
    with pytest.raises(ValueError,match='identity'):
        flow_rows(payload,'BK0574',DAY,config())
    payload['data']['klines']=[line.replace(',30,',',31,')]
    # Difference of one yuan is permitted rounding; two is not.
    payload['data']['klines']=[line.replace(',30,',',32,')]
    with pytest.raises(ValueError,match='definition'):
        flow_rows(payload,'BK1034',DAY,config())


def test_macro_units_and_current_gold_bar_exclusion():
    data=treasury_rows('Date,2 Yr,10 Yr,30 Yr\n10/15/2026,4.77,5.1,5.43\n',DAY)
    assert data['us10y'][0]['value']==5.1
    def point(day,close): return dict(date=day,open=close,high=close+1,low=close-1,close=close)
    points=[point('2026-10-15',100),point('2026-10-16',110)]
    assert gold_rows('callback('+json.dumps(points)+');',DAY)==[dict(data_date=str(DAY),value=100)]
    html='<title>Europe Brent Spot Price FOB (Dollars per Barrel)</title><table><tr><td>2026 Oct-12 to Oct-16</td><td>100</td><td></td><td>102</td><td>103</td><td></td></tr></table>'
    rows=brent_rows(html,DAY)
    assert [r['data_date'] for r in rows]==['2026-10-12','2026-10-14','2026-10-15']


def test_calendar_epoch_and_original_revision_are_preserved():
    epoch=datetime(2026,10,15,0,tzinfo=CN).timestamp()
    raw=dict(id=1,public_date=epoch,title='CPI',importance=4,country='美国',country_id='US',
             actual='3.1',previous='3.0',forecast='3.2',revised='2.9',unit='%',calendar_type='MD')
    result=calendar_rows(dict(code=20000,data=dict(items=[raw])),DAY,DAY)[0]
    assert result['occurs_at']=='2026-10-14T16:00:00+00:00'
    assert result['published_at'] is None and result['unit']=='%'
    assert result['previous']==3.0 and result['revised']==2.9


def test_policy_dates_and_duplicate_teasers_do_not_become_fake_news():
    html='<li><a href="./202610/content_12345.htm">政策通知标题</a><span>2026-10-11</span></li><div><h5><a href="./202610/content_12346.htm">政策解读标题</a></h5><p><a href="./202610/content_12346.htm">这是摘要而非标题</a></p><span>2026-10-12</span></div>'
    rows=parse_listing(html,'gov',DAY)
    assert len(rows)==2 and all(r['published_at'] is None for r in rows)
    assert rows[0]['title']=='政策解读标题'
    assert any(r['published_date']=='2026-10-11' for r in rows)  # Sunday retained.
    with pytest.raises(ValueError,match='full-date'):
        parse_listing('<li><a href="./202610/content_12345.htm">政策通知标题</a><span>10-11</span></li>','gov',DAY)


def test_last_good_feed_is_retained_after_failure():
    store('sample',dict(source='sample/v1',items=[dict(value=1)]))
    db.feed_attempt('sample','failed','HTTPError')
    result=load('sample')
    assert result['items']==[dict(value=1)] and result['last_attempt_status']=='failed'


def test_membership_pagination_uses_stable_code_order():
    from marketdata.board_data import current_members
    class Source:
        def json(self,url,params):
            assert params['fid']=='f12' and params['fs']=='b:BK1036 f:!50'
            return dict(rc=0,data=dict(total=2,diff=[dict(f12='000001',f13=0,f14='甲'),dict(f12='600519',f13=1,f14='乙')]))
    assert [r['symbol'] for r in current_members(Source(),'BK1036')]==['000001','600519']


def test_gainers_http_paging_and_closed_day_guard():
    item=seed()
    days=sessions('CN',DAY,6,config())
    store('adjusted:'+item['id'],dict(instrument_id=item['id'],source='test/qfq',adjustment='qfq',
          items=[dict(trade_date=d,close=10+i) for i,d in enumerate(days)]))
    with TestClient(app) as client:
        first=client.get('/v1/cn/rankings?kind=stock&period=5d&trade_date=2026-10-15&limit=1',headers=HEADERS)
        assert first.status_code==200
        payload=first.json()
        assert payload['items'][0]['change_5d_pct']==50
        second=client.get('/v1/cn/rankings?kind=stock&period=5d&trade_date=2026-10-15&limit=1&offset=1&snapshot_id='+payload['snapshot_id'],headers=HEADERS)
        assert second.status_code==200 and not second.json()['items']
        assert client.get('/v1/cn/rankings?period=5d&trade_date=2026-10-10',headers=HEADERS).status_code==422


def test_new_year_treasury_window_reads_previous_year():
    from marketdata.macro_assets import fetch_treasury
    class Source:
        def get(self,url,params):
            if '/2027/' in url:return 'Date,2 Yr,10 Yr,30 Yr\n01/04/2027,4,5,6\n'
            return 'Date,2 Yr,10 Yr,30 Yr\n12/31/2026,3,4,5\n'
    result=fetch_treasury(Source(),date(2027,1,4))
    assert [r['data_date'] for r in result['us10y']]==['2026-12-31','2027-01-04']


def test_retired_ladder_and_ths_flows_are_not_advertised():
    with TestClient(app) as client:
        for path in ['/v1/macro/assets','/v1/economic-calendar','/v1/policy-news','/v1/cn/fund-rankings']:
            assert client.get(path).status_code==401
        assert client.get('/v1/cn/limit-up-ladder',headers=HEADERS).status_code==404
        assert client.get('/v1/cn/boards/CN.industry.THS_881121/flows',headers=HEADERS).status_code==404
        caps=client.get('/v1/capabilities',headers=HEADERS).json()
        assert 'ths_board_funds' not in caps and 'limit_up' not in caps['supplemental']


def test_disabled_worker_is_not_reported_as_broken_but_required_worker_is(monkeypatch):
    from marketdata.health import health
    assert health()['worker_mode']=='disabled_by_configuration'
    cfg=dict(config(),automatic_collection=True)
    monkeypatch.setattr('marketdata.health.config',lambda:cfg)
    assert 'worker_heartbeat_stale' in health()['issues']


def test_corrupt_context_json_is_not_served_as_valid_data():
    from fastapi import HTTPException
    with db.connect() as con:
        con.execute("INSERT INTO feed_snapshots VALUES('corrupt',?,?,'complete',NULL)",
                    ('{"source":"test","collected_at":"2026-01-01T00:00:00+00:00","items":[NaN]}',db.now_iso()))
    with pytest.raises(HTTPException) as result:load('corrupt')
    assert result.value.status_code==503
