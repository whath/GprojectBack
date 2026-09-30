"""Compare every paginated close snapshot with a consistent cloud database backup."""
import argparse
import json
import sqlite3
import urllib.request
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--database",required=True,type=Path)
    parser.add_argument("--date",required=True)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    values=dict(x.split("=",1) for x in Path("data/deploy/server-client.env").read_text().splitlines() if "=" in x)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(path):
        req=urllib.request.Request("http://127.0.0.1:18000"+path,headers={"Authorization":"Bearer "+values["API_TOKEN"]})
        with opener.open(req,timeout=30) as response:return json.load(response)
    con=sqlite3.connect(args.database);con.row_factory=sqlite3.Row
    report={"trade_date":args.date,"quotes":{},"ohlc_crosscheck":[]}
    try:
        for kind in ("stock","etf"):
            all_rows=[];offset=0
            while True:
                result=get(f"/v1/cn/quotes?kind={kind}&trade_date={args.date}&limit=500&offset={offset}")
                all_rows.extend(result["items"])
                if len(all_rows)>=result["total"]:break
                assert result["items"],"pagination ended early"
                offset+=500
            expected=[dict(json.loads(r["payload"]),collected_at=r["collected_at"]) for r in con.execute("SELECT payload,collected_at FROM datasets WHERE dataset=? AND trade_date=? ORDER BY record_key",("close_quotes:"+kind,args.date))]
            assert all_rows==expected,"quote API/database mismatch"
            assert len({r["symbol"] for r in all_rows})==len(all_rows)
            result.pop("items");report["quotes"][kind]=result
            for row in all_rows:
                assert row["source_time"].startswith(args.date)
                old=con.execute("SELECT * FROM bars WHERE instrument_id=? AND trade_date=?",(f"CN.{kind}.{row['symbol']}",args.date)).fetchone()
                if old:
                    diffs={key:abs(old[key]-row[key]) for key in ("open","high","low","close")}
                    report["ohlc_crosscheck"].append({"symbol":row["symbol"],"history_source":old["source"],"differences":diffs})
                    assert max(diffs.values())<=0.001,(row["symbol"],diffs)
        report["health"]=get("/v1/health")
        report["history_coverage"]=get("/v1/cn/history-coverage?trade_date="+args.date)
        report["result"]="passed"
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"result":"passed","rows":{k:v["total"] for k,v in report["quotes"].items()},"ohlc_comparisons":len(report["ohlc_crosscheck"])}))
    finally:con.close()


if __name__=="__main__":main()
