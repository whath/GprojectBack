"""Bounded, read-only provider checks; retain metadata, never write market data."""
import json
from datetime import datetime, timezone
from pathlib import Path

import akshare
import requests

from marketdata.provider import AKProvider
from marketdata.settings import config


def main():
    result = {"checked_at":datetime.now(timezone.utc).isoformat(),"akshare_version":akshare.__version__,
              "available_membership_functions":{name:hasattr(akshare,name) for name in (
                  "stock_board_cons_ths","stock_board_industry_cons_ths","stock_board_concept_cons_ths")},"probes":[]}
    result["pypi_latest"] = requests.get("https://pypi.org/pypi/akshare/json",timeout=15).json()["info"]["version"]
    cfg = dict(config(),request_timeout_seconds=20,request_attempts=1,request_spacing_seconds=0)
    # Keep the user's configured provider proxy behavior; no OS network changes.
    for endpoint, kwargs, proxy in (("stock_individual_fund_flow",{"stock":"000001","market":"sz"},False),
                                   ("stock_individual_fund_flow",{"stock":"000001","market":"sz"},True),
                                   ("stock_news_em",{"symbol":"美国"},False),
                                   ("stock_fund_flow_industry",{"symbol":"即时"},False)):
        try:
            rows = AKProvider(dict(cfg,use_system_proxy=proxy)).call(endpoint,**kwargs)
            entry = {"endpoint":endpoint,"status":"response","count":len(rows),"fields":sorted(rows[0]) if rows else [],
                     "latest_date":max((str(r.get("日期",r.get("发布时间",""))) for r in rows),default=None)}
        except Exception as exc:
            entry = {"endpoint":endpoint,"status":"failed","error":str(exc)}
        entry["use_system_proxy"] = proxy
        result["probes"].append(entry)
        print(json.dumps(entry,ensure_ascii=True),flush=True)
    Path("docs/AKSHARE-PROBE-20261004.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
