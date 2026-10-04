import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.events import collect as collect_events, report as event_report, save as save_events
from marketdata.funds import report as funds_report, save as save_funds, validate as validate_funds
from marketdata.history import audit, run_slice as history_slice
from marketdata.lifecycle import publish as publish_lifecycle
from marketdata.pipeline import Collector
from marketdata.settings import config

HEADERS = {"Authorization":"Bearer test-secret-token-that-is-at-least-32-characters"}
STAMP = "2026-09-30T09:00:00+00:00"


def item(symbol="000001"):
    return Collector("CN",date(2026,9,30)).item("stock",symbol,"sample","sz"+symbol)


def raw(day="2026-09-30", **extra):
    return {"日期":day,"开盘":10,"最高":11,"最低":9,"收盘":10,
            "成交量":100,"成交额":1000,"volume_unit":"share",**extra}


def flow(identifier="CN.stock.000001", **extra):
    board = ".industry." in identifier
    result = {"instrument_id" if not board else "board_id":identifier,"currency":"CNY",
              "unit":"yuan","scope":"main","source":"akshare/verified/main/v1","updated_at":STAMP,
              "items":[{"trade_date":"2026-09-30","inflow":200,"outflow":100,"net":100}]}
    if board:
        result["classification_source"]="ths"
    return dict(result,**extra)


def event(**extra):
    base=dict(id="event-1",title="event",summary="source summary",markets=["US","GOLD"],
                importance="high",kind="scheduled",occurs_at="2026-10-01T12:00:00+00:00",
                published_at=STAMP,updated_at=STAMP,expires_at="2026-10-02T12:00:00+00:00",
                source="source",url="https://example.org/source")
    return dict(base,**extra)


@pytest.mark.parametrize("extra,match",[
    ({"成交额":-1},"negative amount"),
    ({"日期":"2026-09-27"},"not a market session"),
    ({"成交量":-1},"negative volume"),
])
def test_bad_bars_fail_before_storage(extra,match):
    with pytest.raises(ValueError,match=match):
        normalize.bars([raw(**extra)],"2026-09-30","stock","CN")


def test_mixed_units_and_duplicate_days_are_rejected():
    with pytest.raises(ValueError,match="mixed volume units"):
        normalize.bars([raw("2026-09-29",volume_unit="lot_100_shares"),raw()],"2026-09-30","stock","CN")
    with pytest.raises(ValueError,match="duplicate"):
        normalize.bars([raw(),raw()],"2026-09-30","stock","CN")


def test_storage_source_pin_and_revision_archive_are_atomic():
    stock=item();db.instrument(stock)
    clean=normalize.bars([raw()],"2026-09-30","stock","CN")
    db.put_bars(stock["id"],clean)
    changed=dict(clean[0],amount=1200)
    db.put_bars(stock["id"],[changed])
    with pytest.raises(ValueError,match="switch"):
        db.put_bars(stock["id"],[dict(changed,source="akshare/sina")])
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM bar_revisions").fetchone()[0]==1
        assert con.execute("SELECT amount FROM bars").fetchone()[0]==1200


def test_catalog_registered_even_if_history_provider_fails(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *a:None)
    class Provider:
        def call(self,endpoint,**kwargs):
            if endpoint=="stock_info_a_code_name":return [{"代码":"000001","名称":"sample"}]
            raise RuntimeError("upstream failed")
    cfg=config();cfg.update(cn_stocks=["000001"],cn_etfs=[],cn_boards={"industry":[],"concept":[]})
    Collector("CN",date(2026,9,30),sample=True,provider=Provider(),settings=cfg).run()
    with TestClient(app) as client:
        data=client.get("/v1/instruments?market=CN&kind=stock",headers=HEADERS).json()
        assert data["total"]==1 and data["items"][0]["id"]=="CN.stock.000001"
        assert client.get("/v1/bars/CN.stock.000001?end=2026-09-30",headers=HEADERS).status_code==503


def test_501_items_stay_stable_across_paging_and_catalog_changes():
    stocks=[item(f"{i:06d}") for i in range(1,502)]
    db.publish_catalog(stocks,"CN","stock","2026-09-30","akshare/catalog",full=True)
    with TestClient(app) as client:
        first=client.get("/v1/instruments?market=CN&kind=stock&limit=500",headers=HEADERS).json()
        db.publish_catalog([item("000000")]+stocks,"CN","stock","2026-09-30","akshare/catalog",full=True)
        second=client.get("/v1/instruments?market=CN&kind=stock&limit=500&offset=500",headers=HEADERS).json()
        assert first["total"]==second["total"]==501
        assert first["snapshot_id"]==second["snapshot_id"]
        assert len(second["items"])==1
        assert len({r["id"] for r in first["items"]+second["items"]})==501
        refreshed=client.get("/v1/instruments?market=CN&kind=stock&limit=500&refresh=true",headers=HEADERS).json()
        assert refreshed["total"]==502


def test_expired_snapshot_cannot_silently_switch_versions():
    db.instrument(item())
    with TestClient(app) as client:
        client.get("/v1/instruments?limit=1",headers=HEADERS)
        with db.connect() as con:
            con.execute("UPDATE query_snapshots SET expires_at='2000-01-01T00:00:00+00:00'")
        assert client.get("/v1/instruments?limit=1&offset=1",headers=HEADERS).status_code==409


def test_catalog_missing_is_not_falsely_called_delisted_and_history_is_retained():
    stocks=[item(),item("000002")]
    db.publish_catalog(stocks,"CN","stock","2026-09-29","akshare/catalog",full=True)
    db.put_bars(stocks[0]["id"],normalize.bars([raw()],"2026-09-30","stock","CN"))
    db.publish_catalog(stocks[1:],"CN","stock","2026-09-30","akshare/catalog",full=True)
    with db.connect() as con:
        assert con.execute("SELECT status FROM instrument_lifecycle WHERE instrument_id=?",(stocks[0]["id"],)).fetchone()[0]=="catalog_missing"
        assert con.execute("SELECT count(*) FROM bars").fetchone()[0]==1
    with TestClient(app) as client:
        assert client.get("/v1/instruments?market=CN&kind=stock",headers=HEADERS).json()["total"]==1


def test_expanded_history_request_starts_before_existing_short_history(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *a:None)
    stock=item();db.instrument(stock)
    rows=normalize.bars([raw("2026-09-29")],"2026-09-30","stock","CN")
    rows[0]["source"]="akshare/tencent";db.put_bars(stock["id"],rows)
    captured=[]
    class Provider:
        def call(self,endpoint,**kw):captured.append(kw["start_date"]);return [raw()]
    cfg=dict(config(),history_days=4000)
    Collector("CN",date(2026,9,30),provider=Provider(),settings=cfg).history(stock,"stock_zh_a_hist_tx")
    assert captured[0] < "20260919"


def test_api_queues_history_expansion_without_waiting_and_filters_legacy_future():
    stock=item();db.instrument(stock)
    rows=normalize.bars([raw()],"2026-09-30","stock","CN");db.put_bars(stock["id"],rows)
    with db.connect() as con:
        con.execute("INSERT INTO bars SELECT instrument_id,'2099-01-05',adjustment,open,high,low,close,volume,volume_unit,amount,change_pct,source,collected_at FROM bars WHERE instrument_id=?",(stock["id"],))
    with TestClient(app) as client:
        data=client.get("/v1/bars/CN.stock.000001?limit=2000",headers=HEADERS).json()
        assert [r["trade_date"] for r in data["items"]]==["2026-09-30"]
    with db.connect() as con:
        assert con.execute("SELECT start_date FROM history_requests").fetchone()[0]<"2020-01-01"


@pytest.mark.parametrize("day", ["2023-12-31", "2022-12-31"])
def test_calendar_year_ending_on_weekend_is_a_closed_session(day):
    from marketdata.calendars import session_close
    assert session_close("CN", date.fromisoformat(day), config()) is None


def test_history_audit_distinguishes_ipo_and_real_suspension():
    stock=dict(item(),listed_date="2026-09-29")
    db.publish_catalog([stock],"CN","stock","2026-09-30","akshare/exchange")
    db.put_bars(stock["id"],normalize.bars([raw("2026-09-29")],"2026-09-30","stock","CN"))
    assert audit(stock["id"],"2026-09-30",config(),250)["missing_dates"]==["2026-09-30"]
    db.put_dataset("suspensions","CN","2026-09-30",[{"key":"000001","symbol":"000001"}])
    assert audit(stock["id"],"2026-09-30",config(),250)["complete"]


def test_explicit_exchange_dates_drive_lifecycle():
    publish_lifecycle([{"证券代码":"000001","证券简称":"sample","上市日期":"2000-01-01","终止上市日期":"2026-09-29"}],
                      "stock_info_sz_delist","证券代码","证券简称","终止上市日期","2026-09-30")
    with db.connect() as con:
        row=con.execute("SELECT * FROM instrument_lifecycle").fetchone()
        assert row["status"]=="delisted" and row["delisted_date"]=="2026-09-29"


@pytest.mark.parametrize("changes",[
    {"currency":"USD"},{"unit":"wan_yuan"},{"scope":"all"},{"source":"estimated/volume"},
    {"instrument_id":"CN.stock.000002"},{"updated_at":"2026-09-30T09:00:00"},
    {"items":[{"trade_date":"2026-09-30","net":float("inf")}]},
    {"items":[{"trade_date":"2026-09-30","net":None}]},
    {"items":[{"trade_date":"2026-09-30","inflow":2,"outflow":1,"net":100}]},
    {"items":[{"trade_date":"2026-09-27","net":1}]},
    {"items":[{"trade_date":"2099-01-05","net":1}]},
])
def test_invalid_funds_reject_the_whole_response(changes):
    with pytest.raises(ValueError):validate_funds(flow(**changes),"CN.stock.000001")


def test_funds_append_limit_end_and_scope_source_pin():
    save_funds(flow(items=[{"trade_date":"2026-09-29","net":-10}]),"CN.stock.000001")
    save_funds(flow(),"CN.stock.000001")
    assert funds_report("CN.stock.000001",2000,date(2026,9,29))["items"]==[{"trade_date":"2026-09-29","inflow":None,"outflow":None,"net":-10}]
    assert len(funds_report("CN.stock.000001",1)["items"])==1
    with pytest.raises(ValueError,match="switch"):
        save_funds(flow(source="akshare/other/main"),"CN.stock.000001")


def test_board_calendar_retains_missing_collection_dates():
    identifier="CN.industry.THS_881121"
    save_funds(flow(identifier),identifier)
    data=funds_report(identifier,20,date(2026,9,30))
    assert len(data["trade_dates"])==20 and len(data["items"])==1
    assert data["trade_dates"][-1]=="2026-09-30"
    assert data["trade_dates"]==sorted(set(data["trade_dates"]))


def test_duplicate_funds_are_rejected():
    data=flow();data["items"]*=2
    with pytest.raises(ValueError,match="duplicate"):save_funds(data,"CN.stock.000001")


def test_new_routes_auth_and_unavailable_statuses_preserve_bars():
    stock=item();db.instrument(stock);db.put_bars(stock["id"],normalize.bars([raw()],"2026-09-30","stock","CN"))
    with TestClient(app) as client:
        paths=("/v1/events","/v1/cn/stocks/CN.stock.000001/flows","/v1/cn/boards/CN.industry.THS_881121/flows")
        for path in paths:
            assert client.get(path).status_code==401
            assert client.get(path,headers=HEADERS).status_code==501
        assert client.get("/v1/bars/CN.stock.000001?end=2026-09-30",headers=HEADERS).status_code==200
        assert client.get("/v1/cn/stocks/US.stock.NVDA/flows",headers=HEADERS).status_code==422
        assert client.get("/v1/cn/boards/CN.industry.BK001/flows",headers=HEADERS).status_code==422


def test_same_day_member_pages_keep_status_and_unique_ids():
    board="CN.industry.BKTEST"
    rows=normalize.members([{"代码":f"{i:06d}","名称":"sample"} for i in range(1,501)])
    db.put_dataset("members:"+board,"CN","2026-09-30",rows)
    Collector("CN",date(2026,9,30)).task("members:"+board,lambda:500)
    with TestClient(app) as client:
        path=f"/v1/cn/boards/{board}/members?observed_date=2026-09-30&limit=500"
        first=client.get(path,headers=HEADERS).json()
        db.put_dataset("members:"+board,"CN","2026-09-30",rows[:10])
        second=client.get(path+"&offset=500",headers=HEADERS).json()
        assert len(first["items"])==500 and second["items"]==[]
        assert second["task_status"][0]["status"]=="complete"
        assert first["snapshot_id"]==second["snapshot_id"]
        assert client.get(path.replace("2026-09-30","2026-09-29"),headers=HEADERS).json()["items"]==[]


def test_event_versions_expiry_and_importance_order():
    now=datetime(2026,10,1,12,tzinfo=timezone.utc)
    old=event(summary="old")
    revised=event(summary="revised",updated_at="2026-10-01T00:00:00+00:00")
    normal=event(id="normal",importance="normal",occurs_at=STAMP)
    expired=event(id="expired",expires_at="2026-10-01T00:00:00+00:00")
    future=event(id="future",published_at="2026-10-02T00:00:00+00:00",updated_at="2026-10-02T00:00:00+00:00")
    save_events({"updated_at":"2026-10-03T00:00:00+00:00","items":[old,revised,normal,expired,future]})
    data=event_report(100,now)
    assert [r["id"] for r in data["items"]]==["event-1","normal"]
    assert data["items"][0]["summary"]=="revised"


@pytest.mark.parametrize("changes",[
    {"markets":["CN"]},{"url":"http://example.org"},{"updated_at":"2026-09-30T00:00:00"},
    {"expires_at":STAMP},{"importance":"urgent"},{"source":""},
])
def test_bad_event_contract_rejected(changes):
    with pytest.raises(ValueError):save_events({"updated_at":STAMP,"items":[event(**changes)]})


def test_news_same_url_merges_markets_and_keeps_old_feed_time():
    class Provider:
        def call(self,*a,**kw):
            return [{"新闻链接":"http://finance.eastmoney.com/a/20260101001.html","新闻标题":"news",
                     "新闻内容":"source text","发布时间":"2026-09-30 17:00:00","文章来源":"publisher"}]
    queries=[{"keyword":"美国","markets":["US"]},{"keyword":"黄金","markets":["GOLD"]}]
    collect_events(Provider(),queries,datetime(2026,10,3,tzinfo=timezone.utc))
    with db.connect() as con:
        data=json.loads(con.execute("SELECT payload FROM feed_snapshots WHERE feed_key='events'").fetchone()[0])
    assert len(data["items"])==1 and data["items"][0]["markets"]==["GOLD","US"]
    assert data["updated_at"]=="2026-09-30T17:00:00+08:00"
    assert event_report(100,datetime(2026,10,3,tzinfo=timezone.utc))["items"]==[]


def test_unknown_ths_members_are_never_marked_complete():
    with TestClient(app) as client:
        response=client.get("/v1/cn/boards/CN.industry.THS_881121/members?observed_date=2026-09-30",headers=HEADERS)
        assert response.status_code==501


@pytest.mark.parametrize("complete",[False,True])
def test_requested_history_worker_checks_internal_gaps_before_completing(monkeypatch,complete):
    stock=item();db.instrument(stock)
    with db.connect() as con:
        con.execute("INSERT INTO history_requests VALUES(?,?,?,?)",(stock["id"],"2026-09-29","2026-09-30",STAMP))
    def history(self,item,endpoint):
        rows=normalize.bars(([raw("2026-09-29")] if complete else [])+[raw()],"2026-09-30","stock","CN")
        db.put_bars(item["id"],rows)
        return rows
    monkeypatch.setattr(Collector,"history",history)
    result=history_slice(config(),datetime(2026,9,30,12,tzinfo=timezone.utc))
    assert result["attempted"]==1
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM history_requests").fetchone()[0]==(0 if complete else 1)
        assert con.execute("SELECT status FROM tasks WHERE scope='requested_history'").fetchone()[0]==("complete" if complete else "pending")


def test_background_queues_rotate_one_at_a_time_and_yield_before_slots(monkeypatch):
    from marketdata.scheduler import maintenance_slice
    calls=[]
    monkeypatch.setattr("marketdata.scheduler._maintenance_cursor",0)
    for module in ("history","backfill","lifecycle","funds","events","economic"):
        monkeypatch.setattr(f"marketdata.{module}.run_slice",lambda cfg,m=module:calls.append(m))
    cfg=dict(config(),cn_history_backfill=True)
    for _ in range(6):maintenance_slice(cfg,datetime(2026,9,30,12,tzinfo=timezone.utc))
    assert len(calls)==6 and len(set(calls))==6
    maintenance_slice(cfg,datetime(2026,9,30,9,8,tzinfo=timezone.utc))
    assert len(calls)==6


def test_failed_subdataset_has_503_without_breaking_overview():
    c=Collector("CN",date(2026,9,30))
    c.task("lhb",lambda:(_ for _ in ()).throw(RuntimeError("upstream failed")))
    c.task("bar:CN.industry.THS_881121",lambda:(_ for _ in ()).throw(RuntimeError("upstream failed")))
    with TestClient(app) as client:
        assert client.get("/v1/cn/lhb?trade_date=2026-09-30",headers=HEADERS).status_code==503
        assert client.get("/v1/cn/rankings?trade_date=2026-09-30",headers=HEADERS).status_code==503
        response=client.get("/v1/overview?cn_date=2026-09-30&us_date=2026-09-29",headers=HEADERS)
        assert response.status_code==200 and response.json()["cn"]["lhb"]["http_status"]==503
        assert "ai" in response.json()["us"]


def test_legacy_bad_amount_is_not_served_as_valid_history():
    stock=item();db.instrument(stock);db.put_bars(stock["id"],normalize.bars([raw()],"2026-09-30","stock","CN"))
    with db.connect() as con:con.execute("UPDATE bars SET amount=-100")
    with TestClient(app) as client:
        assert client.get("/v1/bars/CN.stock.000001?end=2026-09-30",headers=HEADERS).status_code==503


def test_db_lock_has_retriable_status(monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    @contextmanager
    def locked():
        raise sqlite3.OperationalError("database is locked")
        yield
    with TestClient(app) as client:
        monkeypatch.setattr(db,"connect",locked)
        response=client.get("/v1/instruments",headers=HEADERS)
        assert response.status_code==503 and response.headers["Retry-After"]=="5"


def test_event_old_versions_do_not_overwrite_stored_newer_version():
    latest=event(summary="latest",updated_at="2026-10-01T00:00:00+00:00")
    save_events({"updated_at":"2026-10-01T00:00:00+00:00","items":[latest]})
    save_events({"updated_at":"2026-10-02T00:00:00+00:00","items":[event(summary="old")]})
    data=event_report(100,datetime(2026,10,1,1,tzinfo=timezone.utc))
    assert data["items"][0]["summary"]=="latest"


def test_lhb_same_reason_different_report_periods_stay_distinct():
    common={"代码":"000001","名称":"sample","上榜日":"2026-09-30","上榜原因":"连续三个交易日累计涨幅","龙虎榜净买额":100}
    rows=normalize.lhb([dict(common,period_start="2026-09-28",period_end="2026-09-30"),
                        dict(common,period_start="2026-09-29",period_end="2026-09-30")],"2026-09-30")
    assert len({r["key"] for r in rows})==2
