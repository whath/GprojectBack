import json
import sqlite3
from datetime import date,datetime,timedelta,timezone

import pytest
from fastapi.testclient import TestClient

from marketdata import db,normalize
from marketdata.analytics import metrics,us_report
from marketdata.api import app
from marketdata.calendars import CN,ready_at,session_close,validate_collection,due_slots
from marketdata.pipeline import Collector,CollectorLock
from marketdata.settings import config


def raw_bar(day="2026-09-29",close=10):
    return {"日期":day,"开盘":close,"最高":close+1,"最低":close-1,"收盘":close,"成交量":100,"成交额":1000,"涨跌幅":1}


def seed(symbol="XLK",kind="etf",day="2026-09-29",count=21):
    item=dict(id=f"US.{kind}.{symbol}",market="US",kind=kind,symbol=symbol,name=symbol,source_code="105."+symbol,currency="USD")
    db.instrument(item)
    rows=[]
    cursor=date.fromisoformat(day)
    while len(rows)<count:
        if session_close("US",cursor,config()) is not None:
            rows.append(raw_bar(cursor.isoformat(),100-len(rows)))
        cursor-=timedelta(days=1)
    db.put_bars(item["id"],normalize.bars(rows,day,kind,"US"))
    return item


def test_cn_time_gate_and_calendar():
    cfg=config()
    assert session_close("CN",date(2026,10,1),cfg) is None
    assert session_close("CN",date(2026,10,8),cfg) is not None
    assert session_close("CN",date(2026,10,10),cfg) is None
    with pytest.raises(ValueError,match="15:10"):
        validate_collection("CN",date(2026,9,29),cfg,datetime(2026,9,30,15,9,59,tzinfo=CN))
    validate_collection("CN",date(2026,9,30),cfg,datetime(2026,9,30,15,10,tzinfo=CN))


def test_us_dst_and_early_close():
    cfg=config()
    assert ready_at("US",date(2026,9,29),cfg).astimezone(CN).hour==5
    assert ready_at("US",date(2026,12,1),cfg).astimezone(CN).hour==6
    assert ready_at("US",date(2026,11,27),cfg).astimezone(CN).hour==3


def test_scheduler_catchup_only_latest_and_cn_gate():
    cfg=config()
    slots=list(due_slots(cfg,datetime(2026,9,30,22,30,tzinfo=CN)))
    assert [s for s in slots if s[0]=="CN"]==[("CN",date(2026,9,30),"CN:2026-09-30:1910")]
    assert not [s for s in due_slots(cfg,datetime(2026,9,30,15,9,tzinfo=CN)) if s[0]=="CN"]


@pytest.mark.parametrize("hour,minute,expected",[(15,10,"1510"),(17,9,"1510"),(17,10,"1710"),(19,9,"1710"),(19,10,"1910")])
def test_cn_three_schedule_boundaries(hour,minute,expected):
    slots=list(due_slots(config(),datetime(2026,9,30,hour,minute,tzinfo=CN)))
    assert [s[2] for s in slots if s[0]=="CN"]==[f"CN:2026-09-30:{expected}"]


def test_cn_no_holiday_replay():
    assert not [s for s in due_slots(config(),datetime(2026,10,1,17,10,tzinfo=CN)) if s[0]=="CN"]


def test_lhb_reasons_separate_and_no_future_fields():
    common={"代码":"000001","名称":"sample","上榜日":"2026-09-29","龙虎榜买入额":100,"龙虎榜卖出额":50,"龙虎榜净买额":50,"上榜后1日":99}
    rows=normalize.lhb([dict(common,上榜原因="日涨幅"),dict(common,上榜原因="连续三个交易日累计涨幅")],"2026-09-29")
    assert len({r["key"] for r in rows})==2
    assert {r["period"] for r in rows}=={"single_day","multi_day"}
    assert "上榜后1日" not in json.dumps(rows,ensure_ascii=False)
    db.put_dataset("lhb","CN","2026-09-29",rows)
    with pytest.raises(normalize.PendingData):
        normalize.lhb([],"2026-09-29")
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM datasets").fetchone()[0]==2


def test_bad_ohlc_rejected_and_nan_is_null():
    bad=raw_bar()
    bad["最高"]=5
    with pytest.raises(ValueError):
        normalize.bars([bad],"2026-09-29","stock","CN")
    assert normalize.number(float("nan")) is None


def test_idempotent_history_and_backup_restore(tmp_path):
    item=seed()
    seed()
    destination=tmp_path/"backup.sqlite3"
    db.backup(destination)
    with sqlite3.connect(destination) as con:
        assert con.execute("SELECT count(*) FROM bars").fetchone()[0]==21
        assert con.execute("PRAGMA integrity_check").fetchone()[0]=="ok"


def test_metrics_exclude_today_from_volume_average_and_stale_rank():
    item=seed()
    with db.connect() as con:
        con.execute("UPDATE bars SET volume=300 WHERE instrument_id=? AND trade_date='2026-09-29'",(item["id"],))
    assert metrics(item["id"],"2026-09-29")["volume_ratio_20d"]==3
    cfg=config()
    cfg["us_sectors"]={"XLK":"tech","XLF":"finance"}
    report=us_report("2026-09-29",cfg)
    assert report["eligible_count"]==1 and not report["complete"]
    assert us_report("2026-09-30",cfg)["eligible_count"]==0


def test_auth_pagination_dates_and_empty_state():
    item=seed()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code==200
        assert client.get("/v1/instruments").status_code==401
        client.headers["Authorization"]="Bearer test-secret-token-that-is-at-least-32-characters"
        assert client.get("/v1/instruments?limit=999999").status_code==422
        assert client.get(f"/v1/bars/{item['id']}?start=2026-09-30&end=2026-09-01").status_code==422
        response=client.get(f"/v1/bars/{item['id']}")
        assert response.status_code==200 and len(response.json()["items"])==21
        assert "no verified" in client.get("/v1/cn/lhb?trade_date=2026-09-29").json()["empty_means"]
        assert client.get("/v1/us/sectors?trade_date=2026-09-29").json()["complete"] is False


class FakeUS:
    def __init__(self):
        self.fail=False
        self.history_calls=0

    def call(self,endpoint,**kwargs):
        if endpoint=="us_watchlist_catalog":
            return [{"代码":"105."+s} for s in ("XLK","NVDA","SPY")]
        self.history_calls+=1
        if self.fail:
            raise RuntimeError("simulated upstream outage")
        return [raw_bar("2026-09-28",9),raw_bar("2026-09-29",10)]


def small_config():
    cfg=config()
    cfg["us_sectors"]={"XLK":"tech"}
    cfg["us_ai"]={"NVDA":"compute"}
    return cfg


def test_pipeline_resume_and_failed_refresh_preserves_data(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *args:None)
    provider=FakeUS()
    cfg=small_config()
    first=Collector("US",date(2026,9,29),provider=provider,settings=cfg).run()
    assert first["status"]=="complete"
    assert provider.history_calls==3
    Collector("US",date(2026,9,29),provider=provider,settings=cfg).run()
    assert provider.history_calls==3
    provider.fail=True
    assert Collector("US",date(2026,9,29),provider=provider,settings=cfg,refresh=True).run()["status"]=="partial"
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM bars").fetchone()[0]==6
        assert con.execute("SELECT count(*) FROM tasks WHERE status='failed'").fetchone()[0]==3


def test_missing_us_etf_not_marked_complete(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *args:None)
    cfg=small_config()
    cfg["us_sectors"]["MISSING"]="missing"
    assert Collector("US",date(2026,9,29),provider=FakeUS(),settings=cfg).run()["status"]=="partial"


def test_lock_prevents_two_collectors():
    with CollectorLock():
        with pytest.raises(RuntimeError,match="another collector"):
            with CollectorLock():
                pass


def test_target_missing_preserves_history_but_marks_pending(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *args:None)
    cfg=small_config()
    result=Collector("US",date(2026,9,30),provider=FakeUS(),settings=cfg).run()
    assert result["status"]=="partial"
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM tasks WHERE status='pending'").fetchone()[0]==3
        assert con.execute("SELECT count(*) FROM bars").fetchone()[0]==6


class FakeCN:
    def call(self,endpoint,**kwargs):
        if endpoint in ("stock_zh_a_spot_em","fund_etf_spot_em"):
            return [{"代码":s,"名称":s} for s in ("000001","600519","510300","513100")]
        if endpoint.endswith("_name_em"):
            return [{"板块代码":"BKTEST","板块名称":"半导体" if "industry" in endpoint else "人工智能"}]
        if endpoint.endswith("_hist_em") or endpoint=="stock_zh_a_hist":
            import inspect,akshare as ak
            assert kwargs["period"]==inspect.signature(getattr(ak,endpoint)).parameters["period"].default
            return [raw_bar()]
        if endpoint=="stock_lhb_detail_em":
            return [{"代码":"000001","名称":"sample","上榜日":"2026-09-29","上榜原因":"日涨幅","龙虎榜净买额":10}]
        if endpoint=="stock_lhb_jgmmtj_em":
            return [{"代码":"000001","名称":"sample","上榜日期":"2026-09-29","上榜原因":"日涨幅","机构买入净额":10}]
        if endpoint=="stock_lhb_stock_detail_em":
            return [{"序号":1,"交易营业部名称":"机构专用","类型":"日涨幅","买入金额":20,"卖出金额":10,"净额":10}]
        raise AssertionError(endpoint)


def test_cn_pipeline_to_api_lhb_seats_and_bars(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *args:None)
    cfg=config()
    cfg["cn_sources"]={"stock":"eastmoney","etf":"eastmoney","boards":"eastmoney"}
    result=Collector("CN",date(2026,9,29),sample=True,provider=FakeCN(),settings=cfg).run()
    assert result["status"]=="complete"
    with TestClient(app) as client:
        client.headers["Authorization"]="Bearer test-secret-token-that-is-at-least-32-characters"
        assert len(client.get("/v1/cn/lhb?trade_date=2026-09-29").json()["items"])==1
        seats=client.get("/v1/cn/lhb/000001/seats?trade_date=2026-09-29").json()
        assert seats["task_status"][0]["status"]=="complete"
        assert len(seats["items"])==1
        assert len(client.get("/v1/bars/CN.stock.000001").json()["items"])==1
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM datasets WHERE dataset LIKE 'members:%'").fetchone()[0]==0


def test_cn_configured_missing_symbol_marks_partial(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *args:None)
    cfg=config()
    cfg["cn_sources"]={"stock":"eastmoney","etf":"eastmoney","boards":"eastmoney"}
    cfg["cn_stocks"].append("999999")
    assert Collector("CN",date(2026,9,29),sample=True,provider=FakeCN(),settings=cfg).run()["status"]=="partial"


def test_provider_child_metadata_json(monkeypatch,tmp_path):
    import requests
    from marketdata.provider import child
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"rc":0,"data":{"diff":[{"f12":"SPY","f13":107,"f14":"SPY"}]}}
    monkeypatch.setattr(requests,"get",lambda *args,**kwargs:Response())
    request=tmp_path/"input.json"
    response=tmp_path/"output.json"
    request.write_text(json.dumps({"endpoint":"us_watchlist_catalog","kwargs":{"symbols":["SPY"]}}))
    child(request,response)
    assert json.loads(response.read_text(encoding="utf-8"))["rows"][0]["代码"]=="107.SPY"


def test_provider_timeout_is_bounded(monkeypatch):
    import subprocess
    from marketdata.provider import AKProvider,ProviderError
    calls=[]
    def timeout(*args,**kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired("provider",kwargs["timeout"])
    monkeypatch.setattr(subprocess,"run",timeout)
    monkeypatch.setattr("marketdata.provider.time.sleep",lambda _:None)
    with pytest.raises(ProviderError,match="timed out"):
        AKProvider(config()).call("stock_us_hist",symbol="105.NVDA")
    assert calls==[75,75]


def test_missing_intermediate_session_disables_volume_indicator():
    item=seed()
    with db.connect() as con:
        con.execute("DELETE FROM bars WHERE instrument_id=? AND trade_date='2026-09-28'",(item["id"],))
    result=metrics(item["id"],"2026-09-29")
    assert result["volume_ratio_20d"] is None
    assert result["change_1d_pct"] is None


def test_all_negative_sector_performance_not_called_gainers():
    seed()
    with db.connect() as con:
        con.execute("UPDATE bars SET close=50,low=49 WHERE instrument_id='US.etf.XLK' AND trade_date='2026-09-29'")
    cfg=config()
    cfg["us_sectors"]={"XLK":"technology"}
    result=us_report("2026-09-29",cfg)
    assert result["top_label"]=="relative_leaders"
    assert result["top"][0]["change_1d_pct"]<0


def test_sina_fallback_keeps_source_and_sticks_to_series(monkeypatch):
    from marketdata.provider import ProviderError
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *args:None)
    class Fallback:
        calls=[]
        def call(self,endpoint,**kwargs):
            self.calls.append(endpoint)
            if endpoint=="stock_us_hist":
                raise ProviderError("disconnected")
            return [raw_bar()]
    provider=Fallback()
    collector=Collector("US",date(2026,9,29),provider=provider)
    item=collector.item("stock","NVDA","NVDA","105.NVDA")
    collector.history(item,"stock_us_hist")
    collector.history(item,"stock_us_hist")
    assert provider.calls==["stock_us_hist","stock_us_daily","stock_us_daily"]
    with db.connect() as con:
        assert con.execute("SELECT source FROM bars").fetchone()[0]=="akshare/sina"


def test_no_automatic_mixing_of_existing_eastmoney_series(monkeypatch):
    from marketdata.provider import ProviderError
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *args:None)
    item=seed("NVDA","stock")
    class Failed:
        def call(self,endpoint,**kwargs):
            raise ProviderError("disconnected")
    collector=Collector("US",date(2026,9,29),provider=Failed())
    with pytest.raises(normalize.PendingData,match="source switch"):
        collector.history(item,"stock_us_hist")
