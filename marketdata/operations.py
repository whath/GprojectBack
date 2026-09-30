"""Operational status, bounded retries and verified backup retention."""
import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone

from . import db
from .settings import data_dir


def reason(status, message):
    message=message or ""
    if status=="complete": return None,False
    if status in ("running","interrupted"): return status,True
    if "THS membership unavailable" in message: return "unsupported_membership",False
    if "source switch" in message: return "source_conflict",False
    if "collection budget deferred" in message: return "budget_deferred",True
    if "target daily bar absent" in message: return "awaiting_bar",True
    if "target close quote absent" in message: return "awaiting_close_quote",True
    if "empty" in message and any(s in message for s in ("report","LHB","seat")): return "awaiting_disclosure",True
    if "missing from catalog" in message or "not resolved" in message: return "unresolved_instrument",True
    if "ProviderError" in message: return "upstream_error",True
    if status=="pending": return "awaiting_source",True
    return "validation_or_internal_error",False


def alert(key, message=None, severity="warning"):
    now=db.now_iso()
    with db.connect() as con:
        if message is None:
            con.execute("UPDATE operational_alerts SET resolved_at=? WHERE alert_key=? AND resolved_at IS NULL",(now,key))
        else:
            con.execute('''INSERT INTO operational_alerts VALUES(?,?,?,?,?,NULL)
              ON CONFLICT(alert_key) DO UPDATE SET severity=excluded.severity,message=excluded.message,
              last_seen=excluded.last_seen,resolved_at=NULL''',(key,severity,message,now,now))


def heartbeat(state):
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO worker_heartbeat VALUES(1,?,?)",(db.now_iso(),state))


def report_tasks(market, day, scope="configured"):
    with db.connect() as con:
        rows=[dict(r) for r in con.execute("SELECT * FROM tasks WHERE market=? AND trade_date=? AND scope=? ORDER BY task_key",(market,day,scope))]
    for row in rows:
        row["reason_code"],row["retryable"]=reason(row["status"],row["message"])
    return rows


def refresh_alerts(market,day):
    rows=report_tasks(market,day)
    for row in rows:
        key=f"task:{market}:{day}:{row['task_key']}"
        message=None if row["status"]=="complete" else json.dumps({k:row[k] for k in ("status","reason_code","message")},ensure_ascii=False)
        alert(key,message,"info" if row["reason_code"]=="unsupported_membership" else "warning")


def retry_decision(slot_row, cfg, now=None):
    now=now or datetime.now(timezone.utc)
    if slot_row is None: return True
    maximum=1+cfg.get("retry_max_attempts",3)
    if slot_row["attempts"]>=maximum: return False
    if slot_row["status"]=="running": return True  # OS worker + collector locks exclude another process.
    if slot_row["status"]=="complete": return False
    scheduled=slot_row.get("next_retry_at")
    return scheduled is not None and now>=datetime.fromisoformat(scheduled)


def finish_slot(slot, result, cfg, attempts, now=None):
    now=now or datetime.now(timezone.utc)
    rows=report_tasks(result["market"],result["trade_date"])
    retryable=any(r["status"]!="complete" and r["retryable"] for r in rows)
    retryable=retryable or result["status"]=="failed"
    next_time=None
    if retryable and attempts<1+cfg.get("retry_max_attempts",3):
        next_time=(now+timedelta(minutes=cfg.get("retry_interval_minutes",20))).isoformat()
    with db.connect() as con:
        con.execute("UPDATE schedule_slots SET status=?,updated_at=?,next_retry_at=? WHERE slot=?",(result["status"],now.isoformat(),next_time,slot))
    alert("retry_exhausted:"+slot,"Retry limit reached; unresolved data requires inspection" if retryable and next_time is None else None)


def daily_backup(cfg, now=None):
    now=now or datetime.now(timezone.utc)
    folder=data_dir()/"backups"
    folder.mkdir(exist_ok=True)
    destination=folder/f"auto-{now:%Y%m%d}.sqlite3"
    manifest=destination.with_suffix(".json")
    if destination.exists() and manifest.exists():
        try:
            saved=json.loads(manifest.read_text())
            datetime.fromisoformat(saved["created_at"])
            if saved["bytes"]==destination.stat().st_size and len(saved["sha256"])==64:
                return destination
        except (OSError,ValueError,KeyError,TypeError):
            pass
    if shutil.disk_usage(folder).free<512*1024*1024:
        raise RuntimeError("less than 512 MiB free; backup suspended")
    db.backup(destination)
    with destination.open("rb") as handle:
        digest=hashlib.file_digest(handle,"sha256").hexdigest()
    manifest.write_text(json.dumps({"created_at":now.isoformat(),"sha256":digest,"bytes":destination.stat().st_size}),encoding="utf-8")
    # Only rotate files owned by this routine, after a verified new backup exists.
    keep=max(2,int(cfg.get("backup_retention_days",14)))
    for old in sorted(folder.glob("auto-????????.sqlite3"),reverse=True)[keep:]:
        old.unlink()
        old.with_suffix(".json").unlink(missing_ok=True)
    alert("backup_failed")
    return destination
