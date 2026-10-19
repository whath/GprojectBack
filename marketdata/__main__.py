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
    collect.add_argument("--same-source-close",action=argparse.BooleanOptionalAction,default=None,
                         help="use configured validated close adapters; --no-same-source-close is for source diagnostics")
    collect.add_argument("--raw-dir",type=Path)
    commands.add_parser("worker")
    funds=commands.add_parser("collect-funds")
    funds.add_argument("--date",type=date.fromisoformat)
    funds.add_argument("--symbol",action="append")
    commands.add_parser("collect-events")
    supplemental=commands.add_parser("collect-supplemental")
    supplemental.add_argument("--date",type=date.fromisoformat,required=True)
    supplemental.add_argument("--feature",action="append",choices=['indices','minutes','valuation'],required=True)
    supplemental.add_argument("--symbol",action="append")
    supplemental.add_argument("--raw-dir",type=Path)
    scheduled=commands.add_parser("collect-scheduled-events")
    scheduled.add_argument("--year",type=int,required=True)
    scheduled.add_argument("--raw-dir",type=Path)
    context=commands.add_parser('collect-context')
    context.add_argument('--feature',action='append',choices=['macro','calendar','important-calendar','policy','priority-news','boards','gainers'],required=True)
    context.add_argument('--date',type=date.fromisoformat)
    context.add_argument('--max-items',type=int,choices=range(1,501),default=10,metavar='1..500')
    context.add_argument('--scope',choices=['configured','available','catalog'],default='configured')
    context.add_argument('--all-boards',action='store_true')
    context.add_argument('--raw-dir',type=Path)
    history=commands.add_parser("collect-history-batch")
    history.add_argument("--date",type=date.fromisoformat,required=True)
    history.add_argument("--window",type=int,choices=[30],default=30)
    history.add_argument("--max-items",type=int,choices=range(1,501),metavar='1..500',default=100)
    history.add_argument("--budget",type=int,choices=range(30,901),metavar='30..900',default=600)
    history.add_argument("--kind",action='append',choices=['stock','etf'])
    history.add_argument("--scope",choices=['configured','catalog'],default='configured')
    history.add_argument("--raw-dir",type=Path)
    backup=commands.add_parser("backup")
    backup.add_argument("destination",type=Path)
    rebuild=commands.add_parser("rebuild-us-source")
    rebuild.add_argument("instrument_id")
    rebuild.add_argument("--date",type=date.fromisoformat,required=True)
    args=parser.parse_args()
    db.init()
    if args.command=="collect":
        cfg=config()
        day=args.date or latest_ready(args.market,cfg)
        closing=cfg.get('same_source_close_supplements',False) if args.same_source_close is None else args.same_source_close
        result=Collector(args.market,day,args.sample,refresh=args.refresh,
                         settings=dict(cfg,same_source_close_supplements=closing,
                                       raw_response_dir=str(args.raw_dir) if args.raw_dir else None)).run()
        if not args.sample:
            from .operations import refresh_alerts
            refresh_alerts(args.market,day.isoformat())
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result["status"]=="complete" else 2)
    elif args.command=='collect-context':
        from .contextual import run
        result=run(config(),args.feature,args.date,args.max_items,args.scope,args.all_boards,args.raw_dir)
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result['status']=='complete' else 2)
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
    elif args.command=="collect-supplemental":
        from .supplements import run
        result=run(config(),args.date,args.feature,args.symbol,args.raw_dir)
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result['status']=='complete' else 2)
    elif args.command=="collect-scheduled-events":
        from .scheduled import collect
        with CollectorLock():
            result=collect(args.year,args.raw_dir)
        print(json.dumps(result,ensure_ascii=False))
    elif args.command=="collect-history-batch":
        from .manual_history import run
        result=run(config(),args.date,args.window,args.max_items,args.budget,args.kind or ('stock','etf'),args.scope,args.raw_dir)
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result['status']=='complete' else 2)
    elif args.command=="backup":
        db.backup(args.destination)
        print("Backup integrity check: ok")
    elif args.command=="rebuild-us-source":
        from .rebuild import rebuild_us_sina
        print(json.dumps(rebuild_us_sina(args.instrument_id,args.date),ensure_ascii=False))
    elif args.command=="worker":
        if not config().get('automatic_collection',False):
            raise SystemExit('Automatic collection is disabled by configuration; use explicit manual commands')
        from .scheduler import run_once
        from .operations import alert
        with CollectorLock("worker.lock"):
            if config().get('automatic_refresh_enabled',False):
                from .automation import recover
                from datetime import datetime,timezone
                recover(config(),datetime.now(timezone.utc))
            while True:
                try:
                    cfg=config()
                    if not cfg.get('automatic_collection',False):
                        break
                    if cfg.get('automatic_refresh_enabled',False):
                        from .automation import run_once as automatic_refresh
                        automatic_refresh(cfg)
                    else:
                        run_once(cfg)
                    alert("worker_exception")
                except Exception as exc:
                    logging.exception("worker iteration failed; retry in 60 seconds")
                    alert("worker_exception",f"{type(exc).__name__}: {exc}","error")
                time.sleep(5 if config().get('automatic_refresh_enabled',False) else 60)


if __name__=="__main__":
    main()
