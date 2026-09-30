"""Audit real recorded samples, replay writes, then compare live HTTP responses."""
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

from marketdata import db
from marketdata.settings import data_dir
from marketdata.settings import config
from marketdata.calendars import session_close


def snapshot():
    with db.connect() as con:
        bars=[dict(r) for r in con.execute("SELECT * FROM bars ORDER BY instrument_id,trade_date")]
        records=[dict(r) for r in con.execute("SELECT * FROM datasets ORDER BY dataset,market,trade_date,record_key")]
    return bars,records


def business_hash(tables):
    normalized=[[{k:v for k,v in r.items() if k!="collected_at"} for r in rows] for rows in tables]
    return hashlib.sha256(json.dumps(normalized,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def main():
    folder=data_dir()/"acceptance"
    folder.mkdir(exist_ok=True)
    bars,records=snapshot()
    if not bars or not records:
        raise RuntimeError("acceptance requires both real bars and disclosure records")
    by_instrument=defaultdict(list)
    by_dataset=defaultdict(list)
    for row in bars:
        by_instrument[row["instrument_id"]].append(row)
    for row in records:
        by_dataset[(row["dataset"],row["market"],row["trade_date"])].append(row)
    evidence=[json.loads(p.read_text(encoding="utf-8")) for p in sorted((data_dir()/"raw").glob("*.json"),key=lambda p:p.stat().st_mtime)]
    with db.connect() as con:
        instruments={r["id"]:dict(r) for r in con.execute("SELECT * FROM instruments")}
    comparisons=0
    for identifier,stored in by_instrument.items():
        item=instruments[identifier]
        if item["market"]=="US":
            endpoint,symbol="stock_us_daily",item["symbol"]
        else:
            endpoint={"stock":"stock_zh_a_hist_tx","etf":"fund_etf_hist_sina",
                      "industry":"stock_board_industry_index_ths","concept":"stock_board_concept_index_ths"}[item["kind"]]
            symbol=item["source_code"]
            if item["kind"]=="stock" and stored[0]["source"]=="akshare/sina":
                endpoint="stock_zh_a_daily"
        candidates=[e for e in evidence if e["endpoint"]==endpoint and e["parameters"]["symbol"]==symbol and "rows" in e]
        if not candidates:
            raise RuntimeError("raw adapter evidence missing for "+identifier)
        raw={str(r["日期"])[:10]:r for e in candidates for r in e["rows"]}
        for row in stored:
            for field,original in (("open","开盘"),("high","最高"),("low","最低"),("close","收盘"),("volume","成交量")):
                assert row[field]==float(raw[row["trade_date"]][original]),(identifier,row["trade_date"],field)
            assert row["volume_unit"]==raw[row["trade_date"]].get("volume_unit","share")
            assert row["amount"]==raw[row["trade_date"]].get("成交额")
            comparisons+=1
    disclosure_comparisons=0
    seat_comparisons=0
    for (dataset,market,day),stored in by_dataset.items():
        if dataset.startswith("lhb_seats:"):
            _,symbol,direction=dataset.split(":")
            candidate=next(e for e in evidence if e["endpoint"]=="stock_lhb_stock_detail_em"
                and e["parameters"]=={"symbol":symbol,"date":day.replace("-",""),"flag":direction} and "rows" in e)
            for record in stored:
                value=json.loads(record["payload"])
                matches=[r for r in candidate["rows"] if r["交易营业部名称"]==value["seat"] and r["类型"]==value["reason"]]
                assert any(all(value[field]==r.get(original) for field,original in
                    (("buy_amount","买入金额"),("sell_amount","卖出金额"),("net_amount","净额"))) for r in matches)
                seat_comparisons+=1
            continue
        if dataset not in ("lhb","lhb_institutions"):
            continue
        endpoint="stock_lhb_detail_em" if dataset=="lhb" else "stock_lhb_jgmmtj_em"
        candidate=next(e for e in evidence if e["endpoint"]==endpoint and e["parameters"]["start_date"]==day.replace("-","") and "rows" in e)
        source={(str(r["代码"]).zfill(6),r["上榜原因"]):r for r in candidate["rows"]}
        for record in stored:
            value=json.loads(record["payload"])
            raw=source[(value["symbol"],value["reason"])]
            fields=(("buy_amount","龙虎榜买入额"),("sell_amount","龙虎榜卖出额"),("net_amount","龙虎榜净买额")) if dataset=="lhb" else (("buy_amount","机构买入总额"),("sell_amount","机构卖出总额"),("net_amount","机构买入净额"))
            for field,original in fields:
                assert value[field]==raw[original],(dataset,value["symbol"],field)
            assert value["amount_unit"]=="yuan" and value["trade_date"]==day
            disclosure_comparisons+=1
    before=business_hash((bars,records))
    for identifier,stored in by_instrument.items():
        db.put_bars(identifier,stored)
    for (dataset,market,day),stored in by_dataset.items():
        db.put_dataset(dataset,market,day,[json.loads(r["payload"]) for r in stored])
    assert business_hash(snapshot())==before,"replayed ingestion changed business data"
    bars,records=snapshot()
    by_dataset.clear()
    for row in records:
        by_dataset[(row["dataset"],row["market"],row["trade_date"])].append(row)
    backup=folder/"restore-test.sqlite3"
    db.backup(backup)
    with sqlite3.connect(backup) as con:
        con.row_factory=sqlite3.Row
        assert con.execute("SELECT count(*) FROM bars").fetchone()[0]==len(bars)
        assert con.execute("SELECT count(*) FROM datasets").fetchone()[0]==len(records)
        restored=([dict(r) for r in con.execute("SELECT * FROM bars ORDER BY instrument_id,trade_date")],
                  [dict(r) for r in con.execute("SELECT * FROM datasets ORDER BY dataset,market,trade_date,record_key")])
        assert business_hash(restored)==before
    token=secrets.token_urlsafe(32)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    process=subprocess.Popen([sys.executable,"-m","uvicorn","marketdata.api:app","--host","127.0.0.1","--port",str(port)],
        env={**os.environ,"API_TOKEN":token},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    checks=[]
    def request(path,expected=200,authorized=True):
        headers={"Authorization":"Bearer "+token} if authorized else {}
        req=urllib.request.Request(f"http://127.0.0.1:{port}"+path,headers=headers)
        try:
            response=opener.open(req,timeout=10)
        except urllib.error.HTTPError as exc:
            response=exc
        with response:
            assert response.status==expected,(path,response.status)
            body=json.load(response)
        checks.append({"path":path,"status":expected})
        (folder/f"response-{len(checks):02}.json").write_text(json.dumps(body,ensure_ascii=False,indent=2),encoding="utf-8")
        return body
    try:
        for _ in range(100):
            try:
                with opener.open(f"http://127.0.0.1:{port}/healthz",timeout=1):
                    break
            except OSError:
                time.sleep(.1)
        request("/v1/status",401,False)
        request("/v1/instruments?limit=99999",422)
        request("/v1/bars/DOES.NOT.EXIST",404)
        request("/v1/bars/US.stock.NVDA?start=2026-09-30&end=2026-09-01",422)
        all_instruments=request("/v1/instruments?limit=500")
        page=request("/v1/instruments?limit=1&offset=1")
        assert page["items"]==all_instruments["items"][1:2]
        for identifier in by_instrument:
            response=request("/v1/bars/"+identifier+"?limit=2000")
            assert response["items"]==[r for r in bars if r["instrument_id"]==identifier]
        for (dataset,market,day),stored in by_dataset.items():
            if dataset in ("lhb","lhb_institutions"):
                path=f"/v1/cn/lhb?trade_date={day}&limit=500&institutions="+str(dataset=="lhb_institutions").lower()
            elif dataset.startswith("lhb_seats:"):
                _,symbol,direction=dataset.split(":")
                path=f"/v1/cn/lhb/{symbol}/seats?trade_date={day}&direction="+urllib.parse.quote(direction)
            else:
                continue
            response=request(path)
            expected=[dict(json.loads(r["payload"]),collected_at=r["collected_at"]) for r in stored]
            assert response["items"]==expected,(dataset,len(response["items"]),len(expected))
        us_bars=[r for r in bars if r["instrument_id"].startswith("US.")]
        if us_bars:
            day=max(r["trade_date"] for r in us_bars)
            sector=request("/v1/us/sectors?trade_date="+day)
            assert sector["eligible_count"]==2 and not sector["complete"]
            ai=request("/v1/us/ai?trade_date="+day)
            assert ai["eligible_count"]==1 and not ai["complete"]
            assert len([r for r in ai["items"] if r["symbol"]=="NVDA" and r["status"]=="current"])==1
        cn_bars=[r for r in bars if r["instrument_id"].startswith("CN.")]
        if cn_bars:
            day=max(r["trade_date"] for r in cn_bars)
            previous=date.fromisoformat(day)-timedelta(days=1)
            while session_close("CN",previous,config()) is None:
                previous-=timedelta(days=1)
            for kind in ("stock","etf","industry","concept"):
                report=request(f"/v1/cn/rankings?kind={kind}&trade_date={day}")
                expected_ids={r["instrument_id"] for r in cn_bars if r["trade_date"]==day and instruments[r["instrument_id"]]["kind"]==kind}
                assert {r["instrument_id"] for r in report["items"]}==expected_ids
                assert report["ranking_metric"]=="price_change_pct"
                values=[]
                for row in report["items"]:
                    old=next(b for b in bars if b["instrument_id"]==row["instrument_id"] and b["trade_date"]==previous.isoformat())
                    assert abs(row["price_change_pct"]-(row["close"]/old["close"]-1)*100)<1e-9
                    values.append(row["price_change_pct"])
                assert values==sorted(values,reverse=True)
        current=request("/v1/cn/lhb?trade_date=2026-09-30")
        expected=[dict(json.loads(r["payload"]),collected_at=r["collected_at"]) for r in records if r["dataset"]=="lhb" and r["trade_date"]=="2026-09-30"]
        assert current["items"]==expected
        if not expected:
            assert any(s["status"] in ("failed","pending") for s in current["task_status"])
        if cn_bars:
            tasks=request("/v1/tasks?market=CN&trade_date=2026-09-30&scope=sample")
            assert all(r["status"] in ("complete","pending") for r in tasks["items"])
            for row in tasks["items"]:
                if row["task_key"].startswith("bar:") and row["status"]=="pending":
                    assert not request("/v1/bars/"+row["task_key"][4:]+"?start=2026-09-30&end=2026-09-30")["items"]
            members=request("/v1/cn/boards/CN.industry.THS_881121/members?observed_date=2026-09-30")
            assert not members["items"] and members["task_status"][0]["status"]=="pending"
        status=request("/v1/status")
        assert any(r["market"]=="CN" and r["status"]=="partial" for r in status["runs"])
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    result={"verified_at":db.now_iso(),"overall":"partial_acceptance",
        "real_bar_rows":len(bars),"raw_bar_comparisons":comparisons,
        "raw_disclosure_comparisons":disclosure_comparisons,"raw_seat_comparisons":seat_comparisons,"disclosure_and_seat_rows":len(records),
        "idempotent_replay":"passed","backup_restore":"passed","http_checks":checks,
        "verified_scope":"recorded samples only; adapter-to-database-to-live-HTTP values",
        "not_passed":["full configured universe coverage", "CN current-day completeness (see tasks)", "THS complete board membership", "cloud deployment and capacity"]}
    (folder/"report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
