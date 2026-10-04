"""Local API concurrency check and optional live AKShare news acceptance."""
import argparse
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.history import sessions
from marketdata.pipeline import Collector
from marketdata.settings import config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--live-events",action="store_true")
    args=parser.parse_args()
    report={"checked_at":db.now_iso(),"mode":"isolated temporary database; never production"}
    with tempfile.TemporaryDirectory(prefix="gproject-runtime-") as folder:
        old_data,old_token=os.environ.get("DATA_DIR"),os.environ.get("API_TOKEN")
        os.environ["DATA_DIR"]=folder
        os.environ["API_TOKEN"]="isolated-runtime-token-at-least-32-characters"
        try:
            db.init()
            stocks=[Collector("CN",date(2026,9,30)).item("stock",f"{i:06d}","fixture",f"sz{i:06d}") for i in range(1,502)]
            db.publish_catalog(stocks,"CN","stock","2026-09-30","akshare/audit-fixture",full=True)
            days=sessions("CN",date(2026,9,30),250,config())
            raw=[{"日期":d,"开盘":10,"最高":11,"最低":9,"收盘":10,"成交量":100,"成交额":1000,"volume_unit":"share"} for d in days]
            db.put_bars(stocks[0]["id"],normalize.bars(raw,"2026-09-30","stock","CN"))
            with TestClient(app) as client:
                headers={"Authorization":"Bearer "+os.environ["API_TOKEN"]}
                first=client.get("/v1/instruments?market=CN&kind=stock&limit=500",headers=headers).json()
                second=client.get("/v1/instruments?market=CN&kind=stock&limit=500&offset=500",headers=headers).json()
                assert first["total"]==second["total"]==501
                report["catalog"]={"total":501,"page_sizes":[len(first["items"]),len(second["items"])]}
                def query(_):
                    began=time.perf_counter()
                    response=client.get("/v1/bars/CN.stock.000001?end=2026-09-30&limit=250",headers=headers)
                    assert response.status_code==200 and len(response.json()["items"])==250
                    return round(time.perf_counter()-began,4)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    durations=list(pool.map(query,range(4)))
                assert max(durations)<20
                report["four_concurrent_250_bar_requests"]={"seconds":durations,"under_client_20_second_timeout":True,
                                                            "scope":"in-process TestClient; excludes cloud/network"}
                if args.live_events:
                    from marketdata.events import collect
                    from marketdata.provider import AKProvider
                    cfg=dict(config(),request_timeout_seconds=20,request_attempts=1,request_spacing_seconds=0)
                    stored=collect(AKProvider(cfg),cfg["event_queries"])
                    assert client.get("/v1/events").status_code==401
                    response=client.get("/v1/events",headers=headers)
                    assert response.status_code==200
                    data=response.json()
                    report["live_akshare_events"]={"stored":stored,"visible":len(data["items"]),"updated_at":data["updated_at"],
                         "markets":sorted({m for r in data["items"] for m in r["markets"]}),"coverage":data["coverage"]}
        finally:
            for key,old in (("DATA_DIR",old_data),("API_TOKEN",old_token)):
                if old is None:os.environ.pop(key,None)
                else:os.environ[key]=old
    Path("docs/CONTRACT-RUNTIME-20261004.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=="__main__":main()
