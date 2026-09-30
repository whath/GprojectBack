import json
import shutil
from datetime import datetime,timezone

from . import db
from .operations import report_tasks
from .settings import data_dir


def offsite_status(now=None):
    now=now or datetime.now(timezone.utc)
    try:
        saved=json.loads((data_dir()/"offsite-backup.json").read_text(encoding="utf-8"))
        age=(now-datetime.fromisoformat(saved["verified_at"])).total_seconds()
        return {"verified":saved.get("restore_verified") is True,"stale":age>30*3600,
                "age_seconds":age,"last_backup":saved}
    except (OSError,ValueError,KeyError,TypeError):
        return {"verified":False,"stale":True,"age_seconds":None,"last_backup":None}


def health(now=None):
    now=now or datetime.now(timezone.utc)
    with db.connect() as con:
        con.execute("SELECT 1").fetchone()
        heartbeat=con.execute("SELECT * FROM worker_heartbeat WHERE id=1").fetchone()
    age=(now-datetime.fromisoformat(heartbeat["updated_at"])).total_seconds() if heartbeat else None
    manifests=sorted((data_dir()/"backups").glob("auto-????????.json"),reverse=True)
    backup=None
    if manifests:
        try:
            candidate=json.loads(manifests[0].read_text())
            datetime.fromisoformat(candidate["created_at"])
            if manifests[0].with_suffix(".sqlite3").stat().st_size==candidate["bytes"]:
                backup=candidate
        except (OSError,ValueError,KeyError,TypeError):
            pass
    backup_age=(now-datetime.fromisoformat(backup["created_at"])).total_seconds() if backup else None
    free=shutil.disk_usage(data_dir()).free
    issues=[]
    if age is None or age>300: issues.append("worker_heartbeat_stale")
    if backup_age is None or backup_age>30*3600: issues.append("backup_stale")
    if free<1024**3: issues.append("disk_space_low")
    return {"ready":not issues,"database":"ok","worker":dict(heartbeat) if heartbeat else None,
            "heartbeat_age_seconds":age,"backup":backup,"offsite_backup":offsite_status(now),"disk_free_bytes":free,"issues":issues}


def coverage(market,day,cfg,scope="configured"):
    with db.connect() as con:
        snapshots={r["kind"]:dict(r) for r in con.execute("SELECT * FROM universe_snapshots WHERE market=? AND trade_date=? AND scope=?",(market,day,scope))}
        available={r[0] for r in con.execute("SELECT instrument_id FROM bars WHERE trade_date=? AND instrument_id LIKE ?",(day,market+".%"))}
        instruments=[dict(r) for r in con.execute("SELECT * FROM instruments WHERE market=?",(market,))]
    expected={}
    if market=="US":
        expected={"stock":["US.stock."+s for s in cfg["us_ai"]],
                  "etf":["US.etf."+s for s in set(cfg["us_sectors"])|{cfg["us_benchmark"]}]}
    else:
        for kind,key in (("stock","cn_stocks"),("etf","cn_etfs")):
            if not cfg["cn_all_stocks" if kind=="stock" else "cn_all_etfs"]:
                expected[kind]=[f"CN.{kind}.{s}" for s in cfg[key]]
        if not cfg["cn_all_boards"]:
            for kind in ("industry","concept"):
                expected[kind]=[]
                for name in cfg["cn_boards"][kind]:
                    matches=[r["id"] for r in instruments if r["kind"]==kind and name in (r["symbol"],r["name"]) and r["group_name"]==cfg.get("cn_sources",{}).get("boards","eastmoney")]
                    expected[kind]+=matches or [f"UNRESOLVED.{kind}.{name}"]
    categories={}
    for kind in (("stock","etf","industry","concept") if market=="CN" else ("stock","etf")):
        # The day's manifest is authoritative, including newly configured or missing symbols.
        ids=json.loads(snapshots[kind]["expected_ids"]) if kind in snapshots else expected.get(kind)
        if ids is None:
            categories[kind]={"expected":None,"available":None,"complete":False,"reason":"catalog_not_verified"}
        else:
            ids=set(ids); missing=sorted(ids-available)
            categories[kind]={"expected":len(ids),"available":len(ids&available),"complete":not missing,
                "missing_count":len(missing),"missing_ids":missing[:50],"coverage_pct":round(100*len(ids&available)/len(ids),2) if ids else None,
                "catalog_count":snapshots.get(kind,{}).get("catalog_count")}
    tasks=report_tasks(market,day,scope)
    groups={}
    for row in tasks:
        if row["status"]!="complete": groups[row["reason_code"]]=groups.get(row["reason_code"],0)+1
    return {"market":market,"trade_date":day,"scope":scope,"categories":categories,
            "data_complete":all(c["complete"] for c in categories.values()),
            "collection_complete":bool(tasks) and all(t["status"]=="complete" for t in tasks),
            "issue_counts":groups,"issues":[t for t in tasks if t["status"]!="complete"][:100],
            "scope_note":"configured universe only; unsupported membership is separate from bar freshness"}
