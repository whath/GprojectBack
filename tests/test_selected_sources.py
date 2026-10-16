import json
from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.focus_boards import topics, report as focused
from marketdata.history import sessions
from marketdata.intraday import collect as collect_minute, report as minute_report
from marketdata.pipeline import Collector
from marketdata.priority_news import qualified_cn, checked_url, article_date, rss_rows, entry
from marketdata.public_feeds import store
from marketdata.selected_ths import parse_archive
from marketdata.settings import config
from marketdata.tencent_data import minute_rows, etf_rows

DAY=date(2026,10,15)
AUTH={'Authorization':'Bearer test-secret-token-that-is-at-least-32-characters'}


def minute_payload(lines=None,symbol='sz000001'):
    return dict(code=0,data={symbol:dict(data=[dict(date='20261015',data=lines or ['0930 10 100 10000','1500 10 110 11000'])])})


def test_minute_counters_are_differenced_and_source_turnover_revisions_are_withheld():
    points=minute_rows(minute_payload(['0930 10 100 10000','0931 10 100 9999','1500 10 110 11000']),'sz000001',config(),DAY)[str(DAY)]
    assert points[0]['volume']==100 and points[1]['volume']==0 and points[2]['volume']==10
    assert points[1]['amount'] is None and points[1]['amount_counter_revised'] is True
    assert points[1]['cumulative_amount']==9999
    assert all(row['average'] is None for row in points)
    assert minute_rows(minute_payload(['0930 10 100','1500 10 110'],symbol='bj920779'),'bj920779',config(),DAY)[str(DAY)][0]['amount'] is None


@pytest.mark.parametrize('lines',[
    ['0930 10 100','0930 10 110'],['0930 10 100','1500 10 99'],['0930 0 100'],['1500 10 100','0930 10 101'],
])
def test_minute_rejects_duplicate_nonpositive_unordered_and_backwards_volume(lines):
    with pytest.raises(ValueError):minute_rows(minute_payload(lines),'sz000001',config(),DAY)


def test_minute_does_not_invent_lunch_or_after_session_points():
    points=minute_rows(minute_payload(['0930 10 100','1200 10 100','1500 10 110','1510 10 111']),'sz000001',config(),DAY)[str(DAY)]
    assert len(points)==2 and [row['timestamp'][11:16] for row in points]==['09:30','15:00']


def test_minute_identity_and_daily_close_are_cross_checked_and_read_is_offline():
    item=Collector('CN',DAY).item('stock','000001','平安银行','sz000001');db.instrument(item)
    db.put_bars(item['id'],normalize.bars([dict(日期=str(DAY),开盘=10,最高=11,最低=9,收盘=10)],str(DAY),'stock','CN'))
    class Source:
        def json(self,url,params):return minute_payload()
    assert collect_minute(Source(),item['id'],DAY,config())==2
    result=minute_report(item['id'],DAY,config())
    assert result['point_count']==2 and result['collection_on_read'] is False
    with db.connect() as con:
        con.execute('UPDATE bars SET close=10.5 WHERE instrument_id=?',(item['id'],))
    with pytest.raises(HTTPException) as error:minute_report(item['id'],DAY,config())
    assert error.value.status_code==503


def test_etf_parses_publisher_ohlc_order_and_never_accepts_another_symbol():
    item=dict(kind='etf',symbol='510300',source_code='sh510300')
    payload=dict(code=0,data={'sh510300':dict(qt={'sh510300':['name','','510300']},qfqday=[['2026-10-15','10','11','12','9','100']])})
    row=etf_rows(payload,item,DAY,{str(DAY)},'qfq')[0]
    assert (row['open'],row['high'],row['low'],row['close'])==(10,12,9,11)
    assert row['volume_unit']=='source_unit_unverified'
    payload['data']['sh510300']['qt']['sh510300'][2]='510500'
    with pytest.raises(ValueError):etf_rows(payload,item,DAY,{str(DAY)},'qfq')


def test_selected_topics_are_exactly22_and_keep_industry_concept_and_publisher_separate():
    rows=topics();assert len(rows)==len({row['id'] for row in rows})==22
    assert not any(row['topic']=='稳定币' for row in rows)
    assert next(row for row in rows if row['topic']=='AIDC')['id']=='CN.concept.THS_308642'
    assert next(row for row in rows if row['topic']=='数据中心')['id']=='CN.concept.BK0922'
    assert next(row for row in rows if row['topic']=='半导体设备')['kind']=='industry'
    for index,topic in enumerate(rows):
        item=Collector('CN',DAY).item(topic['kind'],topic['symbol'],topic['source_name']);db.instrument(item)
        bars=normalize.bars([dict(日期=d,开盘=10,最高=12,最低=9,收盘=11+index*.01) for d in sessions('CN',DAY,8,config())],str(DAY),topic['kind'],'CN')
        for bar in bars:bar['source']='publisher/'+topic['classification_source']
        db.put_bars(item['id'],bars)
    result=focused(DAY,config())
    assert result['available_count']==22 and result['total']==22
    assert all(row['net_7d'] is None for row in result['items'] if row['classification_source']=='ths')


def test_ths_archive_validates_index_and_year():
    raw='quotebridge_v4_line_bk_885887_01_2026('+json.dumps(dict(data='20261015,10,12,9,11,100,1000'))+')'
    assert parse_archive(raw,'885887',2026)[0]['日期']=='2026-10-15'
    with pytest.raises(ValueError):parse_archive(raw,'886050',2026)
    with pytest.raises(ValueError):parse_archive(raw.replace('20261015','20251015'),'885887',2026)


def test_national_policy_requires_qualified_path_and_title_not_merely_a_gov_domain():
    row=dict(publisher='中国证监会',published_date=str(DAY),title='中国证监会公开征求意见',original_url='https://www.csrc.gov.cn/csrc/c100028/c123456/content.shtml')
    assert qualified_cn(row,'csrc')
    assert not qualified_cn(dict(row,title='湖北证监局发布监管通知'),'csrc')
    assert not qualified_cn(dict(row,title='主席会见企业代表'),'csrc')
    assert not qualified_cn(dict(row,original_url='https://www.csrc.gov.cn/local/notice'),'csrc')
    assert not qualified_cn(dict(row,original_url='https://www.csrc.gov.cn.evil.test/csrc/c100028/c123456/content.shtml'),'csrc')


def test_original_issuer_hosts_publication_dates_and_levels():
    assert checked_url('nvidia','https://blogs.nvidia.com/blog/news/')=='https://blogs.nvidia.com/blog/news/'
    with pytest.raises(ValueError):checked_url('nvidia','https://blogs.nvidia.com.evil.test/blog/news/')
    assert article_date('<meta name="content_publishedAt" content="2026-10-15T04:00:00Z">')=='2026-10-15'
    rows=rss_rows('<rss><channel><item><title>NVIDIA announces new platform</title><link>https://blogs.nvidia.com/blog/news/</link><pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>','nvidia')
    assert rows[0]['authority_level']=='company' and rows[0]['time_precision']=='instant'
    with pytest.raises(ValueError):entry('fed','Future release','https://www.federalreserve.gov/newsevents/x.htm','2099-01-01')


def test_new_endpoints_require_auth_and_missing_snapshots_remain_explicit():
    with TestClient(app) as client:
        for path in ('/v1/focus-boards','/v1/priority-news','/v1/intraday/CN.stock.000001?trade_date=2026-10-15'):
            assert client.get(path).status_code==401
        assert client.get('/v1/priority-news?start=2026-01-01&end=2026-10-15',headers=AUTH).status_code==422
        response=client.get('/v1/focus-boards?trade_date=2026-10-15',headers=AUTH)
        assert response.status_code==200 and response.json()['available_count']==0


def test_existing_tencent_etf_series_is_updated_without_a_source_switch(monkeypatch):
    from marketdata import tencent_data
    collector=Collector('CN',DAY)
    item=collector.item('etf','510300','沪深300ETF','sh510300');db.instrument(item)
    def rows(close):
        result=normalize.bars([dict(日期=str(DAY),开盘=10,最高=12,最低=9,收盘=close)],str(DAY),'etf','CN')
        for row in result:row['source']='public/tencent-etf/v1'
        return result
    db.put_bars(item['id'],rows(10))
    monkeypatch.setattr(tencent_data,'fetch_etf',lambda *args,**kwargs:rows(11))
    result=collector.history(item,'fund_etf_hist_sina')
    assert result[0]['source']=='public/tencent-etf/v1' and result[0]['close']==11
    with db.connect() as con:assert con.execute('SELECT count(*) FROM bar_revisions').fetchone()[0]==1


def test_priority_news_manual_command_refreshes_policy_before_qualification(monkeypatch):
    from marketdata import contextual,policy_news,priority_news
    calls=[]
    monkeypatch.setattr(policy_news,'collect',lambda *args: calls.append('national-pages') or [dict(status='complete')])
    monkeypatch.setattr(priority_news,'collect',lambda *args: calls.append('qualification-and-us') or [dict(status='complete')])
    result=contextual.run(config(),['priority-news'],DAY)
    assert calls==['national-pages','qualification-and-us'] and result['status']=='complete'
    assert result['automatic_jobs_enabled'] is False
