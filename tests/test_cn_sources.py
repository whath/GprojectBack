import json
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.pipeline import Collector
from marketdata.provider import adapt_frame, check_lhb_publication, child
from marketdata.settings import config


@pytest.mark.parametrize("symbol,raw_volume",[("sz000001",12),("sh688099",1200),("sh600519",1200),("sz300750",1200)])
def test_tencent_pinned_akshare_volume_units(symbol,raw_volume):
    frame=pd.DataFrame([dict(date="2026-09-29",open=10,high=11,low=9,close=10,volume=raw_volume,amount=12000)])
    row=adapt_frame("stock_zh_a_hist_tx",frame,{"symbol":symbol}).to_dict("records")[0]
    clean=normalize.bars([row],"2026-09-29","stock","CN")[0]
    assert clean["volume"]==1200 and clean["volume_unit"]=="share"
    assert clean["amount"]==12000


def test_ths_catalog_has_separate_namespace_and_etf_keeps_exchange():
    frame=adapt_frame("stock_board_industry_name_ths",pd.DataFrame([{"name":"半导体","code":"881121"}]),{})
    assert frame.iloc[0]["板块代码"]=="THS_881121"
    frame=adapt_frame("fund_etf_category_sina",pd.DataFrame([{"代码":"sh510300","名称":"ETF"}]),{})
    assert frame.iloc[0]["代码"]=="510300" and frame.iloc[0]["source_code"]=="sh510300"


@pytest.mark.parametrize("payload,pending",[
    ({"code":9201,"success":False,"result":None},True),
    ({"code":500,"success":False,"result":None},False),
    ({"code":0,"success":True,"result":None},False),
])
def test_only_explicit_empty_lhb_is_pending(monkeypatch,payload,pending):
    import requests
    class Response:
        def raise_for_status(self): pass
        def json(self): return payload
    monkeypatch.setattr(requests,"get",lambda *a,**kw:Response())
    with pytest.raises(normalize.PendingData if pending else ValueError) as error:
        check_lhb_publication("stock_lhb_detail_em",dict(start_date="20260930",end_date="20260930"))
    assert isinstance(error.value,normalize.PendingData)==pending


def test_lhb_pending_crosses_subprocess_protocol(monkeypatch,tmp_path):
    def empty(*args):
        raise normalize.PendingData("source report empty (9201)")
    monkeypatch.setattr("marketdata.provider.check_lhb_publication",empty)
    request,response=tmp_path/"in.json",tmp_path/"out.json"
    request.write_text(json.dumps({"endpoint":"stock_lhb_detail_em","kwargs":{}}))
    child(request,response)
    assert "pending" in json.loads(response.read_text())


class AlternateCN:
    def __init__(self): self.calls=[]
    def call(self,endpoint,**kwargs):
        self.calls.append((endpoint,kwargs))
        if endpoint=="stock_info_a_code_name":
            return [{"代码":"000001","名称":"平安银行"}]
        if endpoint=="fund_etf_category_sina":
            assert kwargs=={"symbol":"ETF基金"}
            return [{"代码":"510300","名称":"ETF","source_code":"sh510300"}]
        if endpoint.endswith("_name_ths"):
            return [{"板块代码":"THS_881121" if "industry" in endpoint else "THS_302035",
                     "板块名称":"半导体" if "industry" in endpoint else "人工智能"}]
        if endpoint.startswith("stock_lhb"):
            raise normalize.PendingData("explicit empty report")
        assert "period" not in kwargs
        if endpoint.endswith("_index_ths"):
            assert kwargs["symbol"] in ("半导体","人工智能") and "adjust" not in kwargs
        if endpoint=="stock_zh_a_hist_tx": assert kwargs["symbol"]=="sz000001"
        if endpoint=="fund_etf_hist_sina": assert kwargs=={"symbol":"sh510300"}
        return [{"日期":d,"开盘":p,"最高":p+1,"最低":p-1,"收盘":p,"成交量":100,"volume_unit":"source_unit_unverified"}
                for d,p in (("2026-09-28",10),("2026-09-29",11))]


def test_alternate_cn_pipeline_and_rankings(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *a:None)
    cfg=config();cfg.update(cn_stocks=["000001"],cn_etfs=["510300"])
    provider=AlternateCN()
    result=Collector("CN",date(2026,9,29),sample=True,provider=provider,settings=cfg).run()
    assert result["status"]=="partial"  # Empty disclosures remain unverified.
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM bars").fetchone()[0]==8
        assert con.execute("SELECT count(*) FROM tasks WHERE status='failed'").fetchone()[0]==0
    with TestClient(app) as client:
        client.headers["Authorization"]="Bearer test-secret-token-that-is-at-least-32-characters"
        report=client.get("/v1/cn/rankings?kind=industry&trade_date=2026-09-29").json()
        assert len(report["items"])==1 and report["classification_source"]=="ths"
        assert report["items"][0]["price_change_pct"]==pytest.approx(10)
        assert report["items"][0]["change_pct"] is None
        assert not client.get("/v1/cn/rankings?kind=industry&trade_date=2026-09-29&classification_source=eastmoney").json()["items"]
        assert not client.get("/v1/cn/rankings?kind=industry&trade_date=2026-09-29&metric=change_pct").json()["items"]
        assert not client.get("/v1/cn/rankings?kind=industry&trade_date=2026-09-30").json()["items"]
        with db.connect() as con:
            con.execute("DELETE FROM bars WHERE trade_date='2026-09-28'")
        assert not client.get("/v1/cn/rankings?kind=industry&trade_date=2026-09-29").json()["items"]


def test_cn_source_switch_preserves_existing_series(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *a:None)
    collector=Collector("CN",date(2026,9,29),provider=AlternateCN())
    item=collector.item("stock","000001","平安银行","sz000001")
    collector.history(item,"stock_zh_a_hist_tx")
    with db.connect() as con:
        con.execute("UPDATE bars SET source='akshare/eastmoney'")
    with pytest.raises(normalize.PendingData,match="source switch"):
        collector.history(item,"stock_zh_a_hist_tx")
    with db.connect() as con:
        assert {r[0] for r in con.execute("SELECT source FROM bars")}=={"akshare/eastmoney"}


def test_beijing_stock_routes_to_sina(monkeypatch):
    monkeypatch.setattr("marketdata.pipeline.validate_collection",lambda *a:None)
    class Beijing(AlternateCN):
        def call(self,endpoint,**kwargs):
            if endpoint=="stock_info_a_code_name":
                return [{"代码":"920779","名称":"sample"}]
            if endpoint=="stock_zh_a_daily":
                assert kwargs["symbol"]=="bj920779" and kwargs["adjust"]==""
                assert "period" not in kwargs and "timeout" not in kwargs
                return [{"日期":"2026-09-29","开盘":10,"最高":11,"最低":9,"收盘":10,"成交量":100,"volume_unit":"share"}]
            return super().call(endpoint,**kwargs)
    cfg=config();cfg.update(cn_stocks=["920779"],cn_etfs=["510300"])
    Collector("CN",date(2026,9,29),sample=True,provider=Beijing(),settings=cfg).run()
    with db.connect() as con:
        assert con.execute("SELECT source FROM bars WHERE instrument_id='CN.stock.920779'").fetchone()[0]=="akshare/sina"
