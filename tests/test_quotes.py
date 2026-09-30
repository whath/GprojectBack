import json
from datetime import datetime

from fastapi.testclient import TestClient

from marketdata import db
from marketdata.api import app
from marketdata.calendars import CN
from marketdata.quotes import parse_tencent,report,collect


def wire(code="sh600519",stamp="20260930161458",close="1258.62"):
    values=[""]*40
    for i,v in {2:code[2:],3:close,4:"1235.58",5:"1239.53",6:"38331",30:stamp,32:"1.86",33:"1268",34:"1236.05"}.items(): values[i]=v
    return 'v_'+code+'="'+'~'.join(values)+'";'


def test_dated_parser_preserves_units_and_rejects_invalid():
    rows=parse_tencent(wire(),["sh600519"])
    assert rows[0]["source_time"]=="2026-09-30T16:14:58+08:00"
    assert rows[0]["volume_unit"]=="source_unit_unverified"
    assert parse_tencent(wire(close="0"),["sh600519"])==[]
    assert parse_tencent(wire(stamp="bad"),["sh600519"])==[]
    import pytest
    with pytest.raises(ValueError):parse_tencent(wire()+wire(),["sh600519"])
    with pytest.raises(ValueError):parse_tencent(wire(),["sz000001"])


def test_snapshot_api_auth_pagination_and_incomplete_manifest():
    with db.connect() as con:
        con.execute("INSERT INTO universe_snapshots VALUES(?,?,?,?,?,?,?)",("CN","2026-09-30","quotes","stock",json.dumps(["sh600519","sz000001"]),2,db.now_iso()))
        con.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,?)",("close_quotes:stock","CN","2026-09-30","sh600519","600519",json.dumps(parse_tencent(wire(),["sh600519"])[0]),db.now_iso()))
    with TestClient(app) as client:
        path="/v1/cn/quotes?trade_date=2026-09-30"
        assert client.get(path).status_code==401
        headers={"Authorization":"Bearer test-secret-token-that-is-at-least-32-characters"}
        result=client.get(path,headers=headers).json()
        assert result["expected"]==2 and result["total"]==1 and not result["complete"]
        assert result["missing_symbols"]==["sz000001"]
        assert client.get(path+"&offset=1",headers=headers).json()["items"]==[]


def test_no_stale_or_intraday_quote_published(monkeypatch):
    import marketdata.quotes as quotes
    from datetime import date
    from marketdata.normalize import PendingData
    class Clock:
        @staticmethod
        def now(tz):return datetime(2026,9,30,17,0,tzinfo=CN)
        fromisoformat=datetime.fromisoformat
        strptime=datetime.strptime
    monkeypatch.setattr(quotes,"datetime",Clock)
    class Collector:
        day=date(2026,9,30)
        errors=[]
        def task(self,key,fn,**kwargs):
            try:return fn()
            except PendingData as e:self.errors.append(str(e))
        def call(self,endpoint,**kwargs):
            if endpoint=="cn_suspensions":return []
            if endpoint=="stock_info_a_code_name":return [{"代码":"600519","名称":"test"}]
            if endpoint=="fund_etf_category_sina":return []
            return parse_tencent(wire(stamp="20260929161458"),["sh600519"])
    c=Collector();collect(c)
    assert c.errors and report("2026-09-30","stock")["total"]==0


def test_suspended_security_is_accounted_without_fake_bar():
    with db.connect() as con:
        con.execute("INSERT INTO universe_snapshots VALUES(?,?,?,?,?,?,?)",("CN","2026-09-30","quotes","stock",json.dumps(["sz000016"]),1,db.now_iso()))
    db.put_dataset("suspensions","CN","2026-09-30",[{"key":"000016","symbol":"000016","status":"reported_full_session_suspension"}])
    state=report("2026-09-30","stock")
    assert state["complete"] and state["total"]==0 and state["suspended_count"]==1
    assert not state["historical_series_complete"]
