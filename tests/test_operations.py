import json
from datetime import date,datetime,timedelta,timezone

import pytest
from fastapi.testclient import TestClient

from marketdata import db,normalize
from marketdata.api import app
from marketdata.health import health,coverage
from marketdata.operations import alert,daily_backup,finish_slot,heartbeat,reason,retry_decision
from marketdata.pipeline import Collector
from marketdata.rebuild import rebuild_us_sina
from marketdata.scheduler import run_once
from marketdata.settings import config,data_dir


def raw(day="2026-09-29",price=10):
    return {"日期":day,"开盘":price,"最高":price+1,"最低":price-1,"收盘":price,"成交量":100}


def existing():
    item=Collector("US",date(2026,9,29)).item("etf","XLB","XLB","107.XLB")
    db.instrument(item)
    db.put_bars(item["id"],normalize.bars([raw()],"2026-09-29","etf","US"))
    return item["id"]


def test_rebuild_atomic_archive_and_source_pin():
    identifier=existing()
    class Provider:
        def call(self,*args,**kwargs): return [raw()]
    report=rebuild_us_sina(identifier,date(2026,9,29),Provider())
    assert report["new_rows"]==1
    with db.connect() as con:
        assert con.execute("SELECT source FROM bars").fetchone()[0]=="akshare/sina"
        old=json.loads(con.execute("SELECT old_rows FROM series_rebuilds").fetchone()[0])
        assert old[0]["source"]=="akshare/eastmoney"


@pytest.mark.parametrize("rows",[[],[raw(price=20)],[raw("2026-09-28")]])
def test_rebuild_rejects_loss_or_inconsistent_prices(rows):
    identifier=existing()
    class Provider:
        def call(self,*args,**kwargs): return rows
    with pytest.raises(ValueError): rebuild_us_sina(identifier,date(2026,9,29),Provider())
    with db.connect() as con:
        assert con.execute("SELECT source FROM bars").fetchone()[0]=="akshare/eastmoney"
        assert con.execute("SELECT count(*) FROM series_rebuilds").fetchone()[0]==0


def test_status_separates_unsupported_from_late_and_errors():
    assert reason("pending","THS membership unavailable")==("unsupported_membership",False)
    assert reason("pending","target daily bar absent")==("awaiting_bar",True)
    assert reason("failed","ProviderError: disconnected")==("upstream_error",True)
    assert reason("pending","source switch requires a separate rebuild")==("source_conflict",False)


def test_retry_has_delay_limit_and_does_not_retry_unsupported():
    now=datetime(2026,9,30,11,10,tzinfo=timezone.utc)
    cfg=config();cfg.update(retry_max_attempts=2,retry_interval_minutes=20)
    c=Collector("CN",date(2026,9,30))
    c.task("bar:CN.stock.000001",lambda:(_ for _ in ()).throw(normalize.PendingData("target daily bar absent")))
    with db.connect() as con:
        con.execute("INSERT INTO schedule_slots(slot,status,updated_at,attempts) VALUES(?,'running',?,1)",("CN:2026-09-30:1910",now.isoformat()))
    result={"market":"CN","trade_date":"2026-09-30","status":"partial"}
    finish_slot("CN:2026-09-30:1910",result,cfg,1,now)
    with db.connect() as con: row=dict(con.execute("SELECT * FROM schedule_slots").fetchone())
    assert not retry_decision(row,cfg,now+timedelta(minutes=19))
    assert retry_decision(row,cfg,now+timedelta(minutes=20))
    row["attempts"]=3
    assert not retry_decision(row,cfg,now+timedelta(hours=5))


def test_retry_only_skips_successes_and_unsupported_but_retries_missing():
    c=Collector("CN",date(2026,9,30))
    c.task("done",lambda:1)
    c.task("members",lambda:(_ for _ in ()).throw(normalize.PendingData("THS membership unavailable")))
    c.task("bar",lambda:(_ for _ in ()).throw(normalize.PendingData("target daily bar absent")))
    retry=Collector("CN",date(2026,9,30),retry_only=True)
    calls=[]
    for key in ("done","members","bar"): retry.task(key,lambda k=key:calls.append(k),always=True)
    assert calls==["bar"]


def test_backup_retention_and_no_rotation_on_failure(monkeypatch):
    cfg=config();cfg["backup_retention_days"]=2
    for day in (27,28,29): daily_backup(cfg,datetime(2026,9,day,tzinfo=timezone.utc))
    files=list((data_dir()/"backups").glob("auto-*.sqlite3"))
    assert {p.name for p in files}=={"auto-20260928.sqlite3","auto-20260929.sqlite3"}
    def failed(*a): raise RuntimeError("disk failure")
    monkeypatch.setattr(db,"backup",failed)
    with pytest.raises(RuntimeError): daily_backup(cfg,datetime(2026,9,30,tzinfo=timezone.utc))
    assert all(p.exists() for p in files)


def test_alert_dedup_resolve_and_health():
    alert("same","first");alert("same","second")
    with db.connect() as con: assert con.execute("SELECT count(*) FROM operational_alerts").fetchone()[0]==1
    alert("same")
    assert "worker_heartbeat_stale" in health()["issues"]
    heartbeat("idle");daily_backup(config())
    assert health()["ready"]
    with TestClient(app) as client:
        assert client.get("/readyz").status_code==200
        assert client.get("/v1/alerts").status_code==401
        client.headers["Authorization"]="Bearer test-secret-token-that-is-at-least-32-characters"
        assert client.get("/v1/alerts").json()["items"]==[]
        assert len(client.get("/v1/alerts?include_resolved=true").json()["items"])==1
        overview=client.get("/v1/overview?cn_date=2026-09-30&us_date=2026-09-29")
        assert overview.status_code==200
        assert overview.json()["cn"]["coverage"]["data_complete"] is False


def test_coverage_does_not_infer_entire_market_from_one_bar():
    existing()
    cfg=config();cfg["us_ai"]={};cfg["us_sectors"]={"XLB":"materials","XLC":"communication"}
    result=coverage("US","2026-09-29",cfg)
    assert result["categories"]["etf"]["expected"]==3
    assert result["categories"]["etf"]["available"]==1
    assert not result["data_complete"]


def test_scheduler_restarts_interrupted_slot_and_limits_retries(monkeypatch):
    monkeypatch.setattr("marketdata.scheduler.due_slots",lambda cfg,now:iter([("CN",date(2026,9,30),"CN:2026-09-30:1910")]))
    now=datetime(2026,9,30,11,10,tzinfo=timezone.utc)
    cfg=config();cfg["retry_max_attempts"]=1
    with db.connect() as con:
        con.execute("INSERT INTO schedule_slots(slot,status,updated_at,attempts) VALUES(?,'running',?,1)",("CN:2026-09-30:1910",now.isoformat()))
    class Failed:
        def __init__(self,*a,**kw): assert kw["retry_only"]
        def run(self): return {"market":"CN","trade_date":"2026-09-30","status":"failed"}
    assert len(run_once(cfg,now,Failed))==1
    assert run_once(cfg,now+timedelta(hours=1),Failed)==[]


def test_budget_defers_requests_without_losing_completed_work(monkeypatch):
    c=Collector("CN",date(2026,9,30))
    c.task("done",lambda:1)
    monkeypatch.setattr("marketdata.pipeline.time.monotonic",lambda:c.started+901)
    called=[]
    c.task("later",lambda:called.append(True))
    assert not called
    with db.connect() as con:
        row=con.execute("SELECT * FROM tasks WHERE task_key='later'").fetchone()
        assert row["reason_code"]=="budget_deferred" and row["retryable"]==1
        assert con.execute("SELECT status FROM tasks WHERE task_key='done'").fetchone()[0]=="complete"


def test_migration_keeps_legacy_slots_and_task_rows():
    with db.connect() as con:
        con.execute("DROP TABLE schedule_slots")
        con.execute("CREATE TABLE schedule_slots(slot TEXT PRIMARY KEY,status TEXT NOT NULL,updated_at TEXT NOT NULL)")
        con.execute("INSERT INTO schedule_slots VALUES('CN:2026-09-30:1510','partial','2026-09-30T08:00:00+00:00')")
        con.execute("ALTER TABLE tasks DROP COLUMN reason_code")
        con.execute("ALTER TABLE tasks DROP COLUMN retryable")
        con.execute("INSERT INTO tasks(market,trade_date,scope,task_key,status,updated_at) VALUES('CN','2026-09-30','configured','bar:test','pending','2026-09-30')")
    db.init();db.init()
    with db.connect() as con:
        row=con.execute("SELECT * FROM schedule_slots").fetchone()
        assert row["attempts"]==1 and row["status"]=="partial" and row["next_retry_at"] is None
        assert con.execute("SELECT retryable FROM tasks WHERE task_key='bar:test'").fetchone()[0]==1


def test_us_resolved_identifiers_survive_discovery_outage(monkeypatch):
    existing()
    cfg=config();cfg.update(us_sectors={"XLB":"materials"},us_ai={},us_benchmark="XLB",us_source_codes={})
    c=Collector("US",date(2026,9,29),settings=cfg)
    def forbidden(*a,**k): raise AssertionError("known ticker must not call discovery")
    monkeypatch.setattr(c,"call",forbidden)
    monkeypatch.setattr(c,"history",lambda *a:1)
    c.us()
    with db.connect() as con:
        assert con.execute("SELECT status FROM tasks WHERE task_key='catalog:us'").fetchone()[0]=="complete"


def test_health_reports_missing_or_corrupt_backup_without_crashing():
    heartbeat("idle")
    destination=daily_backup(config())
    destination.unlink()
    assert "backup_stale" in health()["issues"]
    destination.with_suffix(".json").write_text("{broken")
    assert "backup_stale" in health()["issues"]
    daily_backup(config())
    assert health()["ready"]


def test_offsite_age_and_corrupt_marker():
    from marketdata.health import offsite_status
    from marketdata.settings import data_dir
    import json
    now=datetime(2026,9,30,12,0,tzinfo=timezone.utc)
    marker=data_dir()/"offsite-backup.json"
    marker.write_text("{broken")
    assert offsite_status(now)["stale"]
    marker.write_text(json.dumps({"verified_at":(now-timedelta(hours=31)).isoformat(),"restore_verified":True}))
    assert offsite_status(now)["verified"] and offsite_status(now)["stale"]


def test_manual_success_reconciles_future_retry_and_old_alert(monkeypatch):
    now=datetime(2026,9,30,11,10,tzinfo=timezone.utc)
    slot="CN:2026-09-30:1910"
    monkeypatch.setattr("marketdata.scheduler.due_slots",lambda cfg,now:iter([("CN",date(2026,9,30),slot)]))
    Collector("CN",date(2026,9,30)).task("manual-fixed",lambda:1)
    alert("retry_exhausted:"+slot,"old failure")
    with db.connect() as con:
        con.execute("INSERT INTO schedule_slots(slot,status,updated_at,attempts,next_retry_at) VALUES(?,'partial',?,2,?)",(slot,now.isoformat(),(now+timedelta(hours=1)).isoformat()))
    def forbidden(*a,**kw): raise AssertionError("already repaired; must not re-collect")
    assert run_once(config(),now,forbidden)==[]
    with db.connect() as con:
        row=con.execute("SELECT * FROM schedule_slots").fetchone()
        assert row["status"]=="complete" and row["next_retry_at"] is None
        assert con.execute("SELECT resolved_at FROM operational_alerts WHERE alert_key=?",("retry_exhausted:"+slot,)).fetchone()[0]
