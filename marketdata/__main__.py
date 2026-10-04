import argparse
import json
import logging
import time
from datetime import date
from pathlib import Path

from . import db
from .calendars import due_slots, latest_ready
from .pipeline import Collector,CollectorLock
from .settings import config


def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    parser=argparse.ArgumentParser(description="EOD collection and API operations")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("init")
    collect=commands.add_parser("collect")
    collect.add_argument("--market",choices=["CN","US"],required=True)
    collect.add_argument("--date",type=date.fromisoformat)
    collect.add_argument("--sample",action="store_true")
    collect.add_argument("--refresh",action="store_true",help="also re-fetch successful history tasks")
    commands.add_parser("worker")
    funds=commands.add_parser("collect-funds")
    funds.add_argument("--date",type=date.fromisoformat)
    funds.add_argument("--symbol",action="append")
    commands.add_parser("collect-events")
    backup=commands.add_parser("backup")
    backup.add_argument("destination",type=Path)
    rebuild=commands.add_parser("rebuild-us-source")
    rebuild.add_argument("instrument_id")
    rebuild.add_argument("--date",type=date.fromisoformat,required=True)
    args=parser.parse_args()
    db.init()
    if args.command=="collect":
        day=args.date or latest_ready(args.market,config())
        result=Collector(args.market,day,args.sample,refresh=args.refresh).run()
        if not args.sample:
            from .operations import refresh_alerts
            refresh_alerts(args.market,day.isoformat())
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result["status"]=="complete" else 2)
    elif args.command=="collect-funds":
        from .funds import run_slice
        result=run_slice(config(),args.date,args.symbol)
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result["status"]=="complete" else 2)
    elif args.command=="collect-events":
        from .events import run_slice
        run_slice(config())
        with db.connect() as con:
            row=con.execute("SELECT status,message FROM feed_snapshots WHERE feed_key='events'").fetchone()
        print(json.dumps(dict(row) if row else {"status":"not_configured"},ensure_ascii=False))
        raise SystemExit(0 if row and row["status"]=="complete" else 2)
    elif args.command=="backup":
        db.backup(args.destination)
        print("Backup integrity check: ok")
    elif args.command=="rebuild-us-source":
        from .rebuild import rebuild_us_sina
        print(json.dumps(rebuild_us_sina(args.instrument_id,args.date),ensure_ascii=False))
    elif args.command=="worker":
        from .scheduler import run_once
        from .operations import alert
        with CollectorLock("worker.lock"):
            while True:
                try:
                    run_once(config())
                    alert("worker_exception")
                except Exception as exc:
                    logging.exception("worker iteration failed; retry in 60 seconds")
                    alert("worker_exception",f"{type(exc).__name__}: {exc}","error")
                time.sleep(60)


if __name__=="__main__":
    main()
