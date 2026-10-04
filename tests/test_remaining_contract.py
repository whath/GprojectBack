import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.events import collect as collect_events, report as event_report, save as save_events
from marketdata.funds import collect_stock, report as funds_report, save as save_funds
from marketdata.pipeline import Collector
from tests.test_contract_repairs import HEADERS, STAMP, event, flow, item, raw


def test_confirmed_delisting_survives_generic_catalog_refresh_and_absence():
    stock=dict(item(),listing_status="delisted",listed_date="2000-01-01",delisted_date="2026-09-29")
    db.publish_catalog([stock],"CN","stock","2026-09-29","akshare/exchange")
    db.publish_catalog([item(),item("000002")],"CN","stock","2026-09-30","akshare/catalog",full=True)
    db.publish_catalog([item("000002")],"CN","stock","2026-09-30","akshare/catalog",full=True)
    with db.connect() as con:
        saved=dict(con.execute("SELECT * FROM instrument_lifecycle WHERE instrument_id=?",(stock["id"],)).fetchone())
    assert saved["status"]=="delisted" and saved["delisted_date"]=="2026-09-29"
    assert saved["source"]=="akshare/exchange"


def test_stale_catalog_cannot_erase_current_membership():
    db.publish_catalog([item()],"CN","stock","2026-09-30","akshare/catalog",full=True)
    with pytest.raises(ValueError,match="backwards"):
        db.publish_catalog([item("000002")],"CN","stock","2026-09-29","akshare/catalog",full=True)
    with db.connect() as con:assert con.execute("SELECT count(*) FROM instruments").fetchone()[0]==1


def test_duplicate_company_listing_dates_preserve_evidence_without_inventing_ipo():
    from marketdata.lifecycle import publish
    common={"公司代码":"600190","公司简称":"退市公司","暂停上市日期":"2025-07-28"}
    assert publish([dict(common,上市日期="1998-05-19"),dict(common,上市日期="1999-06-09")],
                   "stock_info_sh_delist","公司代码","公司简称","暂停上市日期","2026-09-30")==1
    with db.connect() as con:
        item=con.execute("SELECT * FROM instrument_lifecycle").fetchone()
        evidence=json.loads(con.execute("SELECT payload FROM lifecycle_observations").fetchone()[0])
    assert item["status"]=="delisted" and item["listed_date"] is None
    assert evidence["reported_listing_dates"]==["1998-05-19","1999-06-09"]


def test_catalog_coverage_requires_all_exchange_routes_and_detects_extra_symbols():
    from marketdata.lifecycle import ROUTES,publish,report
    for number,(endpoint,kwargs,code,name,delist) in enumerate(ROUTES[:4]):
        symbol=["600000","688001","000001","920001"][number]
        row={code:symbol,name:"sample","A股上市日期" if code=="A股代码" else "上市日期":"2020-01-01"}
        publish([row],endpoint,code,name,delist,"2026-09-30",endpoint+":"+str(kwargs.get("symbol","")))
        assert report()["exchange_catalogs_complete"]==(number==3)
    assert report()["directory_complete"]
    db.instrument(item("000009"))
    assert not report()["directory_complete"] and report()["unverified_ids"]==["CN.stock.000009"]


def test_symbol_mapping_cannot_query_another_stock_under_requested_id():
    with pytest.raises(ValueError,match="source symbol"):
        db.instrument(dict(item(),source_code="sz000002"))


def test_adjusted_legacy_rows_do_not_fill_unadjusted_coverage_or_leak_into_api():
    from marketdata.history import audit
    from marketdata.settings import config
    db.instrument(item())
    rows=normalize.bars([raw()],"2026-09-30","stock","CN")
    db.put_bars(item()["id"],rows)
    with db.connect() as con:con.execute("UPDATE bars SET adjustment='qfq'")
    assert audit(item()["id"],"2026-09-30",config(),1)["missing_count"]==1
    with TestClient(app) as client:
        data=client.get("/v1/bars/CN.stock.000001?end=2026-09-30&limit=1",headers=HEADERS).json()
        assert data["items"]==[] and data["adjustment"]=="none"


def test_completed_funds_can_refresh_with_bounded_attempts(monkeypatch):
    from marketdata.funds import run_slice
    from marketdata.settings import config
    db.instrument(item())
    c=Collector("CN",date(2026,9,30));c.scope="funds"
    c.task("flows:CN.stock.000001",lambda:1)
    with db.connect() as con:con.execute("UPDATE tasks SET updated_at=?",(STAMP,))
    calls=[]
    monkeypatch.setattr("marketdata.funds.collect_stock",lambda *a:calls.append(1) or 1)
    assert run_slice(config(),date(2026,9,30))["attempted"]==1 and calls==[1]
    assert run_slice(config(),date(2026,9,30))["attempted"]==0
    with db.connect() as con:con.execute("UPDATE tasks SET attempts=3,updated_at=?",(STAMP,))
    assert run_slice(config(),date(2026,9,30))["attempted"]==0


def test_explicit_snapshot_survives_another_scan_refresh_and_is_query_bound():
    db.instrument(item());db.instrument(item("000002"))
    with TestClient(app) as client:
        first=client.get("/v1/instruments?limit=1",headers=HEADERS).json()
        db.instrument(item("000000"))
        fresh=client.get("/v1/instruments?limit=1&refresh=true",headers=HEADERS).json()
        old=client.get(f"/v1/instruments?limit=1&offset=1&snapshot_id={first['snapshot_id']}",headers=HEADERS).json()
        assert old["total"]==2 and old["items"][0]["symbol"]=="000002"
        assert fresh["total"]==3 and fresh["snapshot_id"]!=first["snapshot_id"]
        assert client.get(f"/v1/instruments?kind=stock&snapshot_id={first['snapshot_id']}",headers=HEADERS).status_code==409
        assert client.get(f"/v1/instruments?refresh=true&snapshot_id={first['snapshot_id']}",headers=HEADERS).status_code==422


def test_corrupted_complete_member_count_is_not_published():
    board="CN.industry.BK0001"
    db.put_dataset("members:"+board,"CN","2026-09-30",[{"key":"000001","symbol":"000001","name":"sample"}])
    Collector("CN",date(2026,9,30)).task("members:"+board,lambda:2)
    with TestClient(app) as client:
        assert client.get(f"/v1/cn/boards/{board}/members?observed_date=2026-09-30",headers=HEADERS).status_code==503
        assert client.get(f"/v1/cn/boards/{board}/members?observed_date=2099-01-01",headers=HEADERS).status_code==422


@pytest.mark.parametrize("feed,change,path",[
    ("flows:CN.stock.000001",lambda p:p["items"][0].update(net=float("inf")),"/v1/cn/stocks/CN.stock.000001/flows"),
    ("flows:CN.stock.000001",lambda p:p.update(instrument_id="CN.stock.000002"),"/v1/cn/stocks/CN.stock.000001/flows"),
    ("events",lambda p:p["items"][0].update(url="http://example.org"),"/v1/events"),
])
def test_corrupt_feed_storage_returns_repair_status(feed,change,path):
    payload={"updated_at":STAMP,"items":[event()]} if feed=="events" else flow()
    change(payload)
    with db.connect() as con:
        con.execute("INSERT INTO feed_snapshots VALUES(?,?,?,'complete',NULL)",(feed,json.dumps(payload),STAMP))
    with TestClient(app) as client:assert client.get(path,headers=HEADERS).status_code==503


def test_board_calendar_window_excludes_old_available_rows():
    board="CN.industry.THS_881121"
    save_funds(flow(board,items=[{"trade_date":"2026-09-01","net":1}]),board)
    data=funds_report(board,2,date(2026,9,30))
    assert data["trade_dates"]==["2026-09-29","2026-09-30"] and data["items"]==[] and data["stale"]


def test_same_funds_version_cannot_change_a_stored_value():
    save_funds(flow(),"CN.stock.000001")
    with pytest.raises(ValueError,match="conflicting"):
        save_funds(flow(items=[{"trade_date":"2026-09-30","net":200}]),"CN.stock.000001")


def test_news_search_rolloff_does_not_remove_verified_market_association():
    class Provider:
        def call(self,*a,**kw):
            return [{"新闻链接":"https://finance.eastmoney.com/a/test.html","新闻标题":"title",
                     "新闻内容":"summary","发布时间":"2026-09-30 17:00:00","文章来源":"publisher"}]
    collect_events(Provider(),[{"keyword":"美国","markets":["US"]},{"keyword":"黄金","markets":["GOLD"]}])
    collect_events(Provider(),[{"keyword":"美国","markets":["US"]}])
    data=event_report(100,datetime(2026,10,1,tzinfo=timezone.utc))
    assert data["items"][0]["markets"]==["GOLD","US"]


def test_inconsistent_main_funds_components_are_rejected(monkeypatch):
    c=Collector("CN",date(2026,9,30))
    monkeypatch.setattr(c,"call",lambda *a,**k:[{"日期":"2026-09-30","主力净流入-净额":100,"大单净流入-净额":20,"超大单净流入-净额":30}])
    with pytest.raises(ValueError,match="definition mismatch"):collect_stock(c,item())


def test_akshare_close_snapshot_keeps_provider_date_and_source(monkeypatch):
    from marketdata.quotes import fetch
    monkeypatch.setattr("akshare.stock_zh_a_hist_tx",lambda **k:pd.DataFrame([raw("2026-09-29"),raw()]))
    rows=fetch(["sz000001"],"2026-09-30","stock")
    assert len(rows)==1 and rows[0]["trade_date"]=="2026-09-30"
    assert rows[0]["source"]=="akshare/stock_zh_a_hist_tx" and rows[0]["previous_close"]==10
    assert "not a quote timestamp" in rows[0]["source_time_basis"]


def test_akshare_suspension_does_not_promote_intraday_or_resume_to_full_session(monkeypatch):
    from marketdata.suspensions import fetch
    common={"名称":"sample","停牌截止时间":None,"预计复牌时间":None,"停牌原因":"reason"}
    records=[dict(common,代码="000001",停牌时间="2026-09-30",停牌期限="盘中停牌"),
             dict(common,代码="000002",停牌时间="2026-09-29",停牌期限="连续停牌"),
             dict(common,代码="000003",停牌时间="2026-09-29",停牌期限="连续停牌",预计复牌时间="2026-09-30")]
    monkeypatch.setattr("akshare.stock_tfp_em",lambda **k:pd.DataFrame(records))
    assert [r["symbol"] for r in fetch("2026-09-30")]==["000002"]


def test_deployment_waiting_does_not_change_authenticated_business_routes():
    with TestClient(app) as client:
        assert client.get("/v1/deployment-status").status_code==401
        state=client.get("/v1/deployment-status",headers=HEADERS).json()
        assert state["icp_filing"]["label"]=="等待落地" and not state["server_changes_performed"]
        assert client.get("/v1/capabilities",headers=HEADERS).status_code==200


def test_economic_facts_do_not_invent_event_publication_or_refresh_unchanged_version():
    from marketdata.economic import collect,report
    class Provider:
        def call(self,*a,**k):
            return [{"日期":"2026-10-05","时间":"22:00","地区":"美国","事件":"ISM服务业指数",
                     "公布":None,"预期":50,"前值":49,"重要性":3}]
    collect(Provider(),"2026-10-05")
    first=report("2026-10-05",100)
    collect(Provider(),"2026-10-05")
    second=report("2026-10-05",100)
    row=second["items"][0]
    assert row["published_at"] is None and row["original_url"] is None and row["occurs_at"] is None
    assert row["source_clock"]=="22:00" and row["markets"]==["US"]
    assert first["updated_at"]==second["updated_at"] and first["items"][0]["updated_at"]==row["updated_at"]
    with TestClient(app) as client:
        assert client.get("/v1/economic-calendar?trade_date=2026-10-05",headers=HEADERS).status_code==200
        assert client.get("/v1/events",headers=HEADERS).status_code==501
