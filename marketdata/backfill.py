"""Resumable low-priority full-catalog historical collection between scheduled runs."""
import json
import time
from datetime import date, datetime, timezone, timedelta

from . import db
from .calendars import CN,CN_COLLECTION_TIMES,validate_collection
from .pipeline import Collector,CollectorLock
from .operations import heartbeat


def run_slice(cfg,now=None):
    now=now or datetime.now(timezone.utc)
    local=now.astimezone(CN)
    if local.hour<15 or (local.hour==15 and local.minute<10):return
    # Leave room for the longest bounded provider call and scheduler sleep.
    if any(timedelta(0)<=datetime.combine(local.date(),t,CN)-local<timedelta(minutes=5) for t in CN_COLLECTION_TIMES):return
    with db.connect() as con:
        day=con.execute("SELECT max(trade_date) FROM universe_snapshots WHERE market='CN' AND scope='quotes'").fetchone()[0]
        if not day:return
        manifests=con.execute("SELECT * FROM universe_snapshots WHERE market='CN' AND scope='quotes' AND trade_date=?",(day,)).fetchall()
        metadata={r["record_key"]:json.loads(r["payload"]) for r in con.execute("SELECT * FROM datasets WHERE trade_date=? AND dataset IN ('close_quotes:stock','close_quotes:etf')",(day,))}
        old={r["task_key"]:dict(r) for r in con.execute("SELECT * FROM tasks WHERE market='CN' AND trade_date=? AND scope='full_history'",(day,))}
        existing={r["instrument_id"] for r in con.execute("SELECT instrument_id FROM bars WHERE trade_date=?",(day,))}
    validate_collection("CN",date.fromisoformat(day),cfg,now)
    candidates=[]
    for manifest in manifests:
        kind=manifest["kind"]
        codes=json.loads(manifest["expected_ids"])
        identifiers=[f"CN.{kind}.{code[2:]}" for code in codes]
        with db.connect() as con:
            con.execute("INSERT OR REPLACE INTO universe_snapshots VALUES(?,?,?,?,?,?,?)",("CN",day,"full_history",kind,json.dumps(identifiers),len(identifiers),db.now_iso()))
        for code,identifier in zip(codes,identifiers):
            state=old.get("bar:"+identifier,{})
            if identifier in existing or state.get("status")=="complete":continue
            if state.get("attempts",0)>=cfg.get("history_backfill_attempts",3):continue
            if state.get("status") not in ("running","interrupted") and state.get("updated_at") and now-datetime.fromisoformat(state["updated_at"])<timedelta(hours=1):continue
            priority=-1 if state.get("status") in ("running","interrupted") else state.get("attempts",0)
            candidates.append((priority,state.get("updated_at",""),code,kind,metadata.get(code,{}).get("name",code[2:])))
    # Untouched entries first: one broken symbol cannot starve later stocks or ETFs.
    candidates.sort(key=lambda r:(r[0],r[1],r[2][2:]))
    settings=dict(cfg,collection_budget_seconds=min(120,cfg.get("history_backfill_seconds",120)),request_attempts=1,request_timeout_seconds=45)
    c=Collector("CN",date.fromisoformat(day),settings=settings,progress=heartbeat)
    c.scope="full_history"
    count=0
    with CollectorLock():
        with db.connect() as con:
            con.execute("UPDATE tasks SET status='interrupted' WHERE market='CN' AND scope='full_history' AND status='running'")
        for _,_,code,kind,name in candidates:
            if time.monotonic()-c.started>=settings["collection_budget_seconds"]:break
            endpoint="fund_etf_hist_sina" if kind=="etf" else "stock_zh_a_daily" if code.startswith("bj") else "stock_zh_a_hist_tx"
            item=c.item(kind,code[2:],name,code)
            c.task("bar:"+item["id"],lambda i=item,e=endpoint:c.history(i,e))
            count+=1
    heartbeat("idle")
    return {"trade_date":day,"attempted":count,"queued_before_slice":len(candidates)}


def report(day):
    from .suspensions import saved
    suspended=saved(day)
    with db.connect() as con:
        manifests=con.execute("SELECT * FROM universe_snapshots WHERE market='CN' AND trade_date=? AND scope='full_history'",(day,)).fetchall()
        latest={r[0] for r in con.execute("SELECT DISTINCT instrument_id FROM bars WHERE trade_date=?",(day,))}
        initialized={r[0] for r in con.execute("SELECT DISTINCT instrument_id FROM bars")}
        states=[dict(r) for r in con.execute("SELECT status,count(*) AS count FROM tasks WHERE market='CN' AND trade_date=? AND scope='full_history' GROUP BY status",(day,))]
    categories={}
    for manifest in manifests:
        ids=set(json.loads(manifest["expected_ids"]))
        absent=ids-latest
        no_trade={identifier for identifier in absent if identifier.rsplit('.',1)[-1] in suspended}
        categories[manifest["kind"]]={"expected":len(ids),"has_history":len(ids&initialized),"target_day_available":len(ids&latest),"suspended_count":len(no_trade),"missing_target_count":len(absent-no_trade)}
    return {"trade_date":day,"scope":"full_history","categories":categories,"tasks":states,
            "complete":bool(categories) and all(v["missing_target_count"]==0 for v in categories.values()),
            "complete_basis":"target-day bar or reported full-session suspension; not full historical continuity",
            "note":"has_history does not certify every historical trading day; late sources remain pending"}
