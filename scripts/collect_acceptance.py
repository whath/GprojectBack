"""Run bounded real samples and preserve upstream adapter responses for audit."""
import argparse
import hashlib
import json
import logging
from datetime import date

from marketdata.calendars import latest_ready
from marketdata.pipeline import Collector
from marketdata.pipeline import CollectorLock
from marketdata import db
from marketdata.provider import AKProvider
from marketdata.settings import config,data_dir


class RecordingProvider(AKProvider):
    def call(self,endpoint,**kwargs):
        request={"endpoint":endpoint,"parameters":kwargs}
        folder=data_dir()/"raw"
        folder.mkdir(exist_ok=True)
        key=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()[:16]
        path=folder/f"{endpoint}-{key}.json"
        try:
            rows=super().call(endpoint,**kwargs)
        except Exception as exc:
            path.write_text(json.dumps({**request,"error":str(exc)},ensure_ascii=False,indent=2),encoding="utf-8")
            raise
        path.write_text(json.dumps({**request,"rows":rows},ensure_ascii=False,indent=2),encoding="utf-8")
        return rows


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--market",choices=["CN","US"],required=True)
    parser.add_argument("--date",type=date.fromisoformat)
    parser.add_argument("--lhb-only",action="store_true")
    parser.add_argument("--refresh",action="store_true",help="re-fetch successful sample tasks for this collection round")
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(message)s")
    cfg=config()
    day=args.date or latest_ready(args.market,cfg)
    collector=Collector(args.market,day,sample=True,provider=RecordingProvider(cfg),settings=cfg,refresh=args.refresh)
    if args.lhb_only:
        if args.market!="CN":
            parser.error("--lhb-only requires --market CN")
        db.init()
        with CollectorLock():
            collector.collect_lhb()
        with db.connect() as con:
            states=[dict(r) for r in con.execute("SELECT task_key,status,row_count,message FROM tasks WHERE market='CN' AND trade_date=? AND scope='sample'",(day.isoformat(),))]
        result={"market":"CN","trade_date":day.isoformat(),"scope":"lhb_sample_only","tasks":states}
    else:
        result=collector.run()
    (data_dir()/f"collection-{args.market}-{day}.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result))


if __name__=="__main__":
    main()
