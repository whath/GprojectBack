"""Exercise deployed operational APIs without printing credentials."""
import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime,timezone
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--base",default="http://127.0.0.1:18000")
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--cn-date",required=True)
    parser.add_argument("--us-date",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    token=dict(line.split("=",1) for line in args.env_file.read_text().splitlines() if "=" in line)["API_TOKEN"]
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    checks=[]
    def get(path,auth=True,expected=200):
        req=urllib.request.Request(args.base+path,headers={"Authorization":"Bearer "+token} if auth else {})
        try: response=opener.open(req,timeout=30)
        except urllib.error.HTTPError as exc: response=exc
        with response:
            assert response.status==expected,(path,response.status)
            assert response.headers.get("Cache-Control")=="no-store"
            payload=json.load(response)
        checks.append({"path":path,"authenticated":auth,"status":expected})
        return payload
    assert get("/readyz",False)["ready"]
    paths=["/v1/health","/v1/alerts","/v1/coverage?market=US&trade_date="+args.us_date,
           "/v1/overview?cn_date="+args.cn_date+"&us_date="+args.us_date]
    for path in paths: get(path,False,401)
    health=get(paths[0]);alerts=get(paths[1]);us=get(paths[2]);overview=get(paths[3])
    cn=get("/v1/coverage?market=CN&trade_date="+args.cn_date)
    assert us["data_complete"] and us["collection_complete"],us["issue_counts"]
    assert sum(c["expected"] for c in us["categories"].values())==24
    assert cn["categories"]["industry"]["expected"]==90
    assert cn["categories"]["concept"]["expected"]==15
    assert overview["cn"]["coverage"]["trade_date"]==args.cn_date
    assert overview["us"]["coverage"]["trade_date"]==args.us_date
    assert health["ready"] and health["backup"]["sha256"]
    assert alerts["delivery"]=="dashboard_only"
    report={"checked_at":datetime.now(timezone.utc).isoformat(),"checks":checks,"health":health,
            "us":us,"cn":cn,"alerts":alerts}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"passed":len(checks),"us_data_complete":us["data_complete"],
                      "cn_data_complete":cn["data_complete"],"alert_count":len(alerts["items"])}))


if __name__=="__main__": main()
