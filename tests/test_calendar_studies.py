from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marketdata import db
from marketdata.api import app
from marketdata.important_calendar import parse_nbs, parse_bea, parse_cn_meeting, parse_bls, report, event
from marketdata.public_feeds import store
from marketdata.watch_quotes import report as watch_quotes
from marketdata.settings import config
from test_revision import seed, HEADERS, DAY


def nbs_fixture():
    header = '<tr><td>序号</td><td>内容</td>' + ''.join(f'<td>{m}月</td>' for m in range(1,13)) + '</tr>'
    body = ''
    titles = ['国民经济运行情况','采购经理指数月度报告','居民消费价格指数月度报告','工业生产者价格指数月度报告']
    for i, title in enumerate(titles):
        # PMI in March contains two releases; February has no release.
        dates = ['19/一'] * 12
        if i == 1: dates[1] = '……'; dates[2] = '4/三 注5 31/二'
        body += '<tr><td rowspan="2">1</td><td rowspan="2">' + title + '</td>' + ''.join(
            '<td rowspan="2">……</td>' if d == '……' else '<td>' + d + '</td>' for d in dates) + '</tr>'
        body += '<tr>' + ''.join('<td>' + ('15:00' if m == 8 else '09:30') + '</td>' for m,d in enumerate(dates,1) if d != '……') + '</tr>'
    return '<h1>2026年国家统计局主要统计信息发布日程表</h1><table>' + header + body + '</table>'


def test_nbs_rowspan_multi_date_and_actual_release_clock():
    rows = parse_nbs(nbs_fixture(),2026)
    assert len(rows) == 48
    pmi = [r for r in rows if r['title'] == '中国官方 PMI']
    assert [r['start_date'] for r in pmi if r['start_date'].startswith('2026-03')] == ['2026-03-04','2026-03-31']
    assert not [r for r in pmi if r['start_date'].startswith('2026-02')]
    assert next(r for r in pmi if r['start_date']=='2026-08-19')['occurs_at']=='2026-08-19T07:00:00+00:00'
    with pytest.raises(ValueError,match='year'): parse_nbs(nbs_fixture(),2027)


def test_bea_gdp_pce_only_and_dst_conversion():
    html='<table><tr><th>Year 2026</th></tr>'
    html+=''.join(f'<tr><td>{day} 8:30 AM</td><td>News</td><td>{title}</td></tr>' for day,title in [
        ('October 29','GDP (Advance Estimate), 3rd Quarter 2026'),
        ('November 25','Personal Income and Outlays, October 2026'),
        ('November 20','Activities of U.S. Multinational Enterprises, 2024')])+'</table>'
    rows=parse_bea(html)
    assert len(rows)==2
    assert rows[0]['occurs_at']=='2026-10-29T12:30:00+00:00'
    assert rows[1]['occurs_at']=='2026-11-25T13:30:00+00:00'


def test_bls_whitelist_and_nonfarm_exact_official_clock():
    rows=parse_bls('<table><tr><td>Friday, November 6, 2026</td><td>08:30 AM</td><td>Employment Situation for October 2026</td></tr><tr><td>Tuesday, November 3, 2026</td><td>10:00 AM</td><td>Job Openings and Labor Turnover Survey for September 2026</td></tr><tr><td>Monday, November 2, 2026</td><td></td><td>Other event</td></tr></table>')
    assert len(rows)==2 and rows[0]['occurs_at']=='2026-11-06T13:30:00+00:00'
    assert rows[1]['occurs_at']=='2026-11-03T15:00:00+00:00' and 'JOLTS' in rows[1]['title']


def test_official_meeting_dynamic_fragment_is_verified_not_empty_success():
    from marketdata.important_calendar import meeting_listing
    class Source:
        def get(self,url):
            return '''<script url="/api-gateway/jpaas-publish-server/front/page/build/unit" queryData="{'parseType':'bulidstatic','webId':'a','tplSetId':'b','pageType':'column','tagId':'list','editType':'null','pageId':'c'}"></script>'''
        def json(self,url,params):
            assert url.startswith('https://www.mofcom.gov.cn/') and params['pageType']=='column'
            return dict(data=dict(html='<a href="/xwfb/ldrhd/art/2026/meeting.html">中共中央政治局召开会议</a>'))
    assert meeting_listing(Source())==['https://www.mofcom.gov.cn/xwfb/ldrhd/art/2026/meeting.html']
    class Empty:
        def get(self,url): return '<html><title>领导人活动</title></html>'
    with pytest.raises(ValueError,match='unverified'): meeting_listing(Empty())


def test_cn_meeting_requires_explicit_date_and_preserves_date_precision():
    url='https://www.mofcom.gov.cn/xwfb/ldrhd/art/2026/example.html'
    rows=parse_cn_meeting('<p>2026-09-21</p><p>会议决定，中国共产党第二十届中央委员会第五次全体会议于10月26日至29日在北京召开。</p>',url)
    assert len(rows)==1 and rows[0]['start_date']=='2026-10-26' and rows[0]['end_date']=='2026-10-29'
    assert rows[0]['occurs_at'] is None and rows[0]['time_precision']=='date'
    assert not parse_cn_meeting('<p>2026-10-16 预计中央经济工作会议于12月举行</p>',url)
    with pytest.raises(ValueError,match='allowlisted'): parse_cn_meeting('','https://example.org/news')


def test_important_calendar_honest_source_status_and_overlap():
    row=event('x','重要会议','CN','meeting','2026-10-26','2026-10-29','https://www.gov.cn','中央','official/test')
    store('important:cn-meetings',dict(source='important/cn-meetings/v1',items=[row]))
    db.feed_attempt('important:cn-meetings','failed','HTTPError')
    data=report(date(2026,10,28),date(2026,10,30),'CN','meeting')
    assert len(data['items'])==1 and data['items'][0]['fetch_stale']
    assert not data['exhaustive'] and any(r['status']=='unavailable' for r in data['sources'])
    assert all(r['start_date'] is None for r in data['pending_meetings'])


def test_only_high_importance_calendar_and_query_override():
    rows=[dict(id=str(i),source='wallstreetcn/macro-calendar/v1',published_at=None,occurs_at='2026-10-15T00:00:00+00:00',
               local_date='2026-10-15',importance=i,country_code='US') for i in range(5)]
    store('calendar:wscn',dict(source='wallstreetcn/macro-calendar/v1',items=rows,start_date='2026-10-01',end_date='2026-10-30'))
    with TestClient(app) as client:
        body=client.get('/v1/economic-calendar?trade_date=2026-10-15',headers=HEADERS).json()
        assert [r['importance'] for r in body['items']]==[3,4]
        assert len(client.get('/v1/economic-calendar?trade_date=2026-10-15&min_importance=0',headers=HEADERS).json()['items'])==5
        for path in ['/v1/watch-quotes?ids=CN.stock.000001','/v1/important-calendar?start=2026-10-01&end=2026-11-01']:
            assert client.get(path).status_code==401


def test_watch_uses_stored_close_outside_rankings_and_has_no_get_side_effects():
    item=seed('000001')
    with db.connect() as con:
        con.execute("UPDATE bars SET close=12,high=13 WHERE instrument_id=? AND trade_date=?",(item['id'],str(DAY)))
        before=con.execute('SELECT count(*) FROM tasks').fetchone()[0]
    rows=watch_quotes([item['id'],'CN.stock.999999'],config(),DAY,DAY)['items']
    assert rows[0]['close']==12 and rows[0]['change_1d_pct']==pytest.approx(20)
    assert rows[0]['price_type']=='stored_daily_close' and rows[1]['status']=='not_found'
    with db.connect() as con: assert con.execute('SELECT count(*) FROM tasks').fetchone()[0]==before


def test_watch_does_not_hide_halts_or_compute_multi_day_return_as_one_day():
    item=seed()
    with db.connect() as con:
        con.execute('DELETE FROM bars WHERE instrument_id=? AND trade_date=?',(item['id'],str(DAY)))
    row=watch_quotes([item['id']],config(),DAY,DAY)['items'][0]
    assert row['status']=='stale' and row['trade_date']<str(DAY) and row['change_1d_pct'] is None
    with pytest.raises(HTTPException): watch_quotes([item['id'],item['id']],config(),DAY,DAY)


from fastapi import HTTPException
