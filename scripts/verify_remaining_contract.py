"""Read-only AKShare source acceptance and isolated real-data API checks."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import akshare
from fastapi.testclient import TestClient

from marketdata import db
from marketdata.api import app
from marketdata.calendars import CN,latest_ready
from marketdata.funds import collect_stock
from marketdata.pipeline import Collector
from marketdata.provider import AKProvider
from marketdata.settings import config


def main():
    cfg=dict(config(),request_timeout_seconds=20,request_attempts=1,request_spacing_seconds=0)
    result={"checked_at":datetime.now(timezone.utc).isoformat(),"akshare_version":akshare.__version__,
            "mode":"read-only source calls; isolated temporary API database; no server changes", "sources":[]}
    before={k:os.environ.get(k) for k in ("DATA_DIR","API_TOKEN")}
    try:
        with tempfile.TemporaryDirectory(prefix="contract-source-") as folder:
            os.environ["DATA_DIR"]=folder
            os.environ["API_TOKEN"]="isolated-source-acceptance-token-at-least-32-characters"
            db.init()
            day=latest_ready("CN",cfg)
            collector=Collector("CN",day,settings=dict(cfg,akshare_transport="curl_cffi"))
            with TestClient(app) as client:
                headers={"Authorization":"Bearer "+os.environ["API_TOKEN"]}
                for symbol in ("000001","600519","688981","920779"):
                    entry={"endpoint":"stock_individual_fund_flow","symbol":symbol,"transport":"curl_cffi"}
                    try:
                        stock=collector.item("stock",symbol,symbol,("sh" if symbol.startswith("6") else "sz" if symbol.startswith("0") else "bj")+symbol)
                        collect_stock(collector,stock)
                        reply=client.get(f"/v1/cn/stocks/CN.stock.{symbol}/flows?end={day}&limit=2000",headers=headers)
                        data=reply.json()
                        assert reply.status_code==200 and data["items"][-1]["trade_date"]==day.isoformat()
                        entry.update(status="validated",count=len(data["items"]),latest_date=data["latest_date"],
                                     source=data["source"],unit=data["unit"],scope=data["scope"],http_status=reply.status_code)
                    except Exception as exc:
                        entry.update(status="failed",error=str(exc)[:200])
                    result["sources"].append(entry)
                    print(json.dumps(entry,ensure_ascii=True),flush=True)
                for endpoint,kwargs in (("stock_tfp_em",{"date":day.strftime("%Y%m%d")}),
                                        ("news_economic_baidu",{"date":"20261005"}),
                                        ("cn_close_quotes",{"symbols":["sz000001"],"day":day.isoformat(),"kind":"stock"}),
                                        ("stock_info_sh_name_code",{"symbol":"主板A股"}),
                                        ("stock_info_sh_name_code",{"symbol":"科创板"}),
                                        ("stock_info_sz_name_code",{"symbol":"A股列表"}),
                                        ("stock_info_bj_name_code",{}),
                                        ("stock_info_sh_delist",{"symbol":"全部"}),
                                        ("stock_info_sz_delist",{"symbol":"终止上市公司"})):
                    entry={"endpoint":endpoint,"transport":"requests"}
                    try:
                        if endpoint=="news_economic_baidu":
                            from marketdata.economic import collect
                            collect(AKProvider(cfg),"2026-10-05")
                            response=client.get("/v1/economic-calendar?trade_date=2026-10-05",headers=headers)
                            assert response.status_code==200
                            data=response.json()
                            entry.update(status="validated_partial_facts",count=data["total"],http_status=response.status_code,
                                         limitations=data["limitations"])
                        else:
                            rows=AKProvider(cfg).call(endpoint,**kwargs)
                            entry.update(status="response",count=len(rows),fields=sorted(rows[0]) if rows else [])
                            if endpoint.startswith("stock_info_"):
                                from marketdata.lifecycle import ROUTES,publish
                                route=next(r for r in ROUTES if r[0]==endpoint and r[1]==kwargs)
                                count=publish(rows,endpoint,route[2],route[3],route[4],datetime.now(CN).date().isoformat(),
                                              endpoint+":"+str(kwargs.get("symbol","")))
                                entry.update(status="validated",accepted_count=count)
                    except Exception as exc:
                        entry.update(status="failed",error=str(exc)[:200])
                    result["sources"].append(entry)
                    print(json.dumps(entry,ensure_ascii=True),flush=True)
                result["ths_functions"]={n:hasattr(akshare,n) for n in (
                    "stock_board_cons_ths","stock_board_industry_cons_ths","stock_board_concept_cons_ths")}
                result["deployment"]=client.get("/v1/deployment-status",headers=headers).json()
                audit=client.get("/v1/catalog-coverage",headers=headers).json()
                result["catalog_coverage"]={k:v for k,v in audit.items() if k not in ("missing_ids","unverified_ids")}
                result["catalog_coverage"].update(missing_count=len(audit["missing_ids"]),unverified_count=len(audit["unverified_ids"]))
    finally:
        for name,value in before.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value
    Path("docs/AKSHARE-CROSSCHECK-20261004.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":main()
