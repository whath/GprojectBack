"""Explicit, validated source rebuild. Failed validation never alters a series."""
import json
from datetime import date,timedelta

from . import db,normalize
from .calendars import validate_collection
from .pipeline import CollectorLock
from .provider import AKProvider
from .settings import config,data_dir


def rebuild_us_sina(identifier,target,provider=None,cfg=None):
    cfg=cfg or config()
    validate_collection("US",target,cfg)
    with CollectorLock():
        with db.connect() as con:
            item=con.execute("SELECT * FROM instruments WHERE id=? AND market='US'",(identifier,)).fetchone()
            old=[dict(r) for r in con.execute("SELECT * FROM bars WHERE instrument_id=? ORDER BY trade_date",(identifier,))]
        if not item or not old: raise ValueError("rebuild requires an existing US series")
        if target.isoformat()<old[-1]["trade_date"]: raise ValueError("target must include existing latest date")
        start=min(old[0]["trade_date"],(target-timedelta(days=cfg["history_days"])).isoformat())
        raw=(provider or AKProvider(cfg)).call("stock_us_daily",symbol=item["symbol"],adjust="")
        rows=normalize.bars([r for r in raw if normalize.day_string(r["日期"])>=start],target.isoformat(),item["kind"],"US")
        indexed={r["trade_date"]:r for r in rows}
        if not rows or rows[-1]["trade_date"]!=target.isoformat(): raise normalize.PendingData("rebuild target date absent; old series retained")
        missing={r["trade_date"] for r in old}-indexed.keys()
        if missing: raise ValueError("replacement lacks existing dates; old series retained")
        max_difference=max(abs(indexed[r["trade_date"]][field]/r[field]-1) for r in old for field in ("open","high","low","close"))
        if max_difference>0.005: raise ValueError("overlap OHLC differs by more than 0.5%; rebuild requires investigation")
        report={"instrument_id":identifier,"old_rows":len(old),"new_rows":len(rows),"source":"akshare/sina","max_ohlc_relative_difference":max_difference}
        backup=data_dir()/"backups"/("before-rebuild-"+identifier+"-"+str(target)+".sqlite3")
        db.backup(backup)
        stamp=db.now_iso()
        with db.connect() as con:
            con.execute("INSERT INTO series_rebuilds(instrument_id,created_at,old_rows,report) VALUES(?,?,?,?)",(identifier,stamp,json.dumps(old),json.dumps(report)))
            con.execute("DELETE FROM bars WHERE instrument_id=?",(identifier,))
            con.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",[
                (identifier,r["trade_date"],"none",r["open"],r["high"],r["low"],r["close"],r["volume"],r["volume_unit"],r["amount"],r["change_pct"],"akshare/sina",stamp) for r in rows])
            con.execute("UPDATE tasks SET status='complete',reason_code=NULL,retryable=0,message=NULL,row_count=?,updated_at=? WHERE market='US' AND trade_date=? AND task_key=?",(len(rows),stamp,target.isoformat(),"bar:"+identifier))
        return report
