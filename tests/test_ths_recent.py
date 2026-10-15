import pytest
from marketdata.ths_recent import parse_recent,merge


def test_ths_recent_checked_overlap_and_no_history_overwrite():
    rows=parse_recent('quotebridge_v4_line_bk_881121_01_last({"data":"20260929,10,12,9,11,100,1000;20260930,11,13,10,12,200,2000"})')
    assert merge(rows[:1],rows,"2026-09-30")==rows
    changed=[dict(rows[0],收盘="10")]
    with pytest.raises(ValueError,match="conflict"):merge(changed,rows,"2026-09-30")
    with pytest.raises(ValueError,match="no overlap"):merge([],rows,"2026-09-30")
    with pytest.raises(ValueError):parse_recent('unexpected({"data":"x"})')


def test_current_ths_candle_requires_identity_date_close_and_previous_session(monkeypatch):
    import json
    from datetime import datetime
    import marketdata.ths_recent as ths
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            from zoneinfo import ZoneInfo
            return datetime(2026,10,15,19,tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(tz) if tz else datetime(2026,10,15,19)
    monkeypatch.setattr(ths,'datetime',Clock)
    point={'1':'20261015','7':'10','8':'12','9':'9','11':'11','13':100,'19':'1000','dt':'1505'}
    def wire(data,code='881121'):
        return f'quotebridge_v4_line_bk_{code}_01_today('+json.dumps({'bk_'+code:data})+')'
    history=[{'日期':'2026-10-14'}]
    row=ths.parse_today(wire(point),'881121','2026-10-15',history)
    assert row['开盘']=='10' and row['收盘']=='11' and row['source_frame']=='ths_v4_today'
    assert row['volume_unit']=='source_unit_unverified'
    for data in [dict(point,dt='1459'),dict(point,dt='2000'),dict(point,**{'1':'20261014'}),dict(point,**{'9':'12'})]:
        with pytest.raises(ValueError):ths.parse_today(wire(data),'881121','2026-10-15',history)
    with pytest.raises(ValueError):ths.parse_today(wire(point,'881122'),'881121','2026-10-15',history)
    with pytest.raises(ValueError):ths.parse_today(wire(point),'881121','2026-10-15',[])


def test_current_archive_candle_is_rechecked_even_when_date_already_exists(monkeypatch):
    import json
    import pandas as pd
    import akshare as ak
    import requests
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import marketdata.ths_recent as ths
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            value=datetime(2026,10,15,19,tzinfo=ZoneInfo('Asia/Shanghai'))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(ths,'datetime',Clock)
    monkeypatch.setattr(ths,'quote_headers',lambda:{})
    monkeypatch.setattr(ak,'stock_board_industry_name_ths',lambda:pd.DataFrame([{'name':'sample','code':'881121'}]))
    yesterday={'日期':'2026-10-14','开盘':'9','最高':'10','最低':'8','收盘':'9.5','成交量':'100','成交额':'950','volume_unit':'source_unit_unverified'}
    cached={'日期':'2026-10-15','开盘':'10','最高':'11','最低':'9.8','收盘':'10.5','成交量':'100','成交额':'1050','volume_unit':'source_unit_unverified'}
    point={'1':'20261015','7':'10','8':'12','9':'9','11':'11','13':300,'19':'3000','dt':'1505'}
    class Response:
        def __init__(self,text):self.text=text
        def raise_for_status(self):pass
    def get(url,**kwargs):
        if url.endswith('last.js'):
            return Response('quotebridge_v4_line_bk_881121_01_last({"data":"20261014,9,10,8,9.5,100,950"})')
        return Response('quotebridge_v4_line_bk_881121_01_today('+json.dumps({'bk_881121':point})+')')
    monkeypatch.setattr(requests,'get',get)
    rows=ths.supplement('stock_board_industry_index_ths',{'symbol':'sample','end_date':'20261015'},[yesterday,cached])
    assert rows[0]==yesterday and rows[-1]['收盘']=='11' and rows[-1]['成交量']==300
    assert rows[-1]['source_frame']=='ths_v4_today'
    point['7']='10.1'
    with pytest.raises(ValueError,match='opening value conflict'):
        ths.supplement('stock_board_industry_index_ths',{'symbol':'sample','end_date':'20261015'},[yesterday,cached])


def test_failed_post_close_check_cannot_publish_a_cached_current_archive_as_complete(monkeypatch,tmp_path):
    import json
    import pandas as pd
    import akshare as ak
    from datetime import datetime
    from marketdata.calendars import CN
    from marketdata.provider import child
    import marketdata.ths_recent as ths
    today=datetime.now(CN).strftime('%Y%m%d')
    monkeypatch.setattr(ak,'stock_board_industry_index_ths',lambda **kw:pd.DataFrame([{'日期':today,'开盘价':10,'最高价':12,'最低价':9,'收盘价':11,'成交量':100,'成交额':1000}]))
    def missing_quote(*a,**kw):raise ValueError('quote unverified')
    monkeypatch.setattr(ths,'supplement',missing_quote)
    request,response=tmp_path/'request.json',tmp_path/'response.json'
    request.write_text(json.dumps(dict(endpoint='stock_board_industry_index_ths',kwargs={'symbol':'sample','end_date':today},same_source_close_supplements=True)))
    child(request,response)
    result=json.loads(response.read_text())
    assert 'post-close candle unverified' in result['pending'] and 'rows' not in result
