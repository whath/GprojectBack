"""Verify an already-running API; never print its bearer token."""
import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--base",default="http://127.0.0.1:8000")
    parser.add_argument("--env-file",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--database",type=Path,help="optional consistent cloud backup for exact row comparison")
    args=parser.parse_args()
    token=os.getenv("API_TOKEN","")
    if args.env_file:
        values=dict(line.split("=",1) for line in args.env_file.read_text().splitlines() if "=" in line)
        token=values["API_TOKEN"]
    assert len(token)>=32
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    checks=[]
    def get(path,auth=True,status=200):
        request=urllib.request.Request(args.base+path,headers={"Authorization":"Bearer "+token} if auth else {})
        try: response=opener.open(request,timeout=20)
        except urllib.error.HTTPError as error: response=error
        with response:
            assert response.status==status,(path,response.status)
            value=json.load(response)
        checks.append({"path":path,"status":status})
        return value
    assert get("/healthz",auth=False)["status"]=="ok"
    get("/v1/status",auth=False,status=401)
    status=get("/v1/status")
    catalog=get("/v1/instruments?limit=500")
    while len(catalog["items"])<catalog["total"]:
        page=get("/v1/instruments?limit=500&offset="+str(len(catalog["items"])))
        assert page["items"],"instrument pagination ended early"
        catalog["items"].extend(page["items"])
    summaries=[]
    connection=sqlite3.connect(args.database) if args.database else None
    if connection:
        connection.row_factory=sqlite3.Row
        assert connection.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    for item in catalog["items"]:
        result=get("/v1/bars/"+item["id"]+"?limit=2000")
        dates=[r["trade_date"] for r in result["items"]]
        assert dates==sorted(set(dates))
        if connection:
            expected=[dict(r) for r in connection.execute("SELECT * FROM bars WHERE instrument_id=? ORDER BY trade_date",(item["id"],))]
            assert result["items"]==expected,item["id"]
        summaries.append({"id":item["id"],"rows":len(dates),"latest":dates[-1] if dates else None})
    if connection:
        from urllib.parse import quote
        for dataset,day in connection.execute("SELECT DISTINCT dataset,trade_date FROM datasets ORDER BY dataset,trade_date"):
            if dataset in ("lhb","lhb_institutions"):
                path=f"/v1/cn/lhb?trade_date={day}&limit=500&institutions="+str(dataset=="lhb_institutions").lower()
            elif dataset.startswith("lhb_seats:"):
                _,symbol,direction=dataset.split(":")
                path=f"/v1/cn/lhb/{symbol}/seats?trade_date={day}&direction="+quote(direction)
            else: continue
            result=get(path)
            expected=[dict(json.loads(r["payload"]),collected_at=r["collected_at"]) for r in connection.execute("SELECT * FROM datasets WHERE dataset=? AND trade_date=? ORDER BY record_key",(dataset,day))]
            assert result["items"]==expected,dataset
        connection.close()
    report={"checks":checks,"runs":status["runs"],"instruments":summaries,"backup_comparison":"passed" if args.database else "not_requested"}
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__": main()
