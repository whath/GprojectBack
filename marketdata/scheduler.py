import logging
from datetime import date,datetime,timedelta,timezone

from . import db
from .calendars import CN,due_slots,session_close
from .operations import alert,heartbeat,report_tasks,retry_decision,finish_slot,refresh_alerts,daily_backup
from .pipeline import Collector


def run_once(cfg,now=None,collector_class=Collector):
    simulated_now=now
    now=now or datetime.now(timezone.utc)
    heartbeat("checking")
    if cfg.get("offsite_backup_expected"):
        from .health import offsite_status
        state=offsite_status(now)
        alert("offsite_backup_stale","No verified offsite backup within 30 hours; local computer must be online" if state["stale"] or not state["verified"] else None)
    try:
        daily_backup(cfg,now)
    except Exception as exc:
        alert("backup_failed",f"{type(exc).__name__}: {exc}","error")
    due=list(due_slots(cfg,now))
    current_slots={s for _,_,s in due}
    backlog_markets=set()
    with db.connect() as con:
        older=[dict(r) for r in con.execute("SELECT * FROM schedule_slots WHERE status!='complete' ORDER BY updated_at")]
    # Recover persisted unfinished dates after downtime; never synthesize a holiday session.
    for row in older:
        market,day_text,_=row["slot"].split(":")
        day=date.fromisoformat(day_text)
        if row["slot"] in current_slots or market in backlog_markets or (now.date()-day).days>14: continue
        if any(m==market and d==day for m,d,_ in due): continue
        if market=="CN" and (now.astimezone(CN).hour<16 or session_close("CN",now.astimezone(CN).date(),cfg) is None): continue
        if retry_decision(row,cfg,now):
            due.append((market,day,row["slot"]))
            backlog_markets.add(market)
    results=[]
    for market,day,slot in due:
        with db.connect() as con:
            existing=con.execute("SELECT * FROM schedule_slots WHERE slot=?",(slot,)).fetchone()
        row=dict(existing) if existing else None
        if row and row["status"] in ("partial","failed"):
            tasks=report_tasks(market,day.isoformat())
            if tasks and all(t["status"]=="complete" for t in tasks):
                with db.connect() as con:
                    con.execute("UPDATE schedule_slots SET status='complete',updated_at=?,next_retry_at=NULL WHERE slot=?",(now.isoformat(),slot))
                refresh_alerts(market,day.isoformat())
                alert("retry_exhausted:"+slot)
                alert("scheduler:"+slot)
                continue
            if row["next_retry_at"] is None and any(t["status"]!="complete" and t["retryable"] for t in tasks):
                # Upgrade old terminal partial slots to the new bounded retry policy.
                row["next_retry_at"]=now.isoformat()
        if not retry_decision(row,cfg,now):
            if row and row["status"]=="running":
                with db.connect() as con:
                    con.execute("UPDATE schedule_slots SET status='failed',next_retry_at=NULL WHERE slot=?",(slot,))
                alert("retry_exhausted:"+slot,"Interrupted collection reached retry limit","error")
            continue
        attempts=(row["attempts"] if row else 0)+1
        with db.connect() as con:
            con.execute('''INSERT INTO schedule_slots(slot,status,updated_at,attempts,next_retry_at)
              VALUES(?,'running',?,?,NULL) ON CONFLICT(slot) DO UPDATE SET
              status='running',updated_at=excluded.updated_at,attempts=excluded.attempts,next_retry_at=NULL''',(slot,now.isoformat(),attempts))
        try:
            result=collector_class(market,day,refresh=(market=="CN" and row is None),settings=cfg,
                                   retry_only=row is not None,progress=heartbeat).run()
            if result["status"] != "failed": alert("scheduler:"+slot)
        except Exception as exc:
            result={"market":market,"trade_date":day.isoformat(),"status":"failed","details":f"{type(exc).__name__}: {exc}"}
            alert("scheduler:"+slot,result["details"],"error")
        if result["status"]=="failed": alert("scheduler:"+slot,result.get("details","Collection failed"),"error")
        finish_slot(slot,result,cfg,attempts,simulated_now)
        refresh_alerts(market,day.isoformat())
        logging.info("slot=%s attempts=%s %s",slot,attempts,result)
        results.append(result)
    if not results and cfg.get("cn_history_backfill") and simulated_now is None:
        from .backfill import run_slice
        run_slice(cfg)
    heartbeat("idle")
    return results
