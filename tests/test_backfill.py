import json
from datetime import datetime,timezone

from marketdata import db
from marketdata.backfill import run_slice,report
from marketdata.settings import config
from marketdata.pipeline import Collector


def seed():
    with db.connect() as con:
        con.execute("INSERT INTO universe_snapshots VALUES(?,?,?,?,?,?,?)",("CN","2026-09-30","quotes","stock",json.dumps(["sh600519","sz000001"]),2,db.now_iso()))


def test_backfill_resumes_without_retrying_completed_or_starving_other_symbols(monkeypatch):
    seed();calls=[]
    monkeypatch.setattr("marketdata.backfill.validate_collection",lambda *a:None)
    def history(self,item,endpoint):
        calls.append(item["symbol"])
        if item["symbol"]=="000001":raise RuntimeError("upstream outage")
        return 1
    monkeypatch.setattr(Collector,"history",history)
    now=datetime(2026,9,30,12,0,tzinfo=timezone.utc)
    run_slice(config(),now)
    assert set(calls)=={"600519","000001"}
    calls.clear()
    run_slice(config(),now)
    assert "600519" not in calls
    state=report("2026-09-30")
    assert state["categories"]["stock"]["expected"]==2
    assert not state["complete"]


def test_backfill_yields_before_fixed_collection_window(monkeypatch):
    seed()
    monkeypatch.setattr(Collector,"history",lambda *a: (_ for _ in ()).throw(AssertionError("must yield")))
    assert run_slice(config(),datetime(2026,9,30,9,8,tzinfo=timezone.utc)) is None


def test_interrupted_work_resumes_before_new_items(monkeypatch):
    seed();calls=[]
    now=datetime(2026,9,30,12,0,tzinfo=timezone.utc)
    with db.connect() as con:
        con.execute("INSERT INTO tasks(market,trade_date,scope,task_key,status,attempts,updated_at) VALUES('CN','2026-09-30','full_history','bar:CN.stock.600519','running',1,?)",(now.isoformat(),))
    monkeypatch.setattr("marketdata.backfill.validate_collection",lambda *a:None)
    monkeypatch.setattr(Collector,"history",lambda self,item,endpoint:calls.append(item["symbol"]) or 1)
    run_slice(config(),now)
    assert calls[0]=="600519"
    with db.connect() as con:
        assert con.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]==0
