"""Finite, resumable recent minute initialization for the listed A-share catalog."""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdata import db
from marketdata.calendars import validate_collection
from marketdata.intraday import collect
from marketdata.pipeline import CollectorLock
from marketdata.settings import config

parser=argparse.ArgumentParser()
parser.add_argument('--date',type=date.fromisoformat,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--workers',type=int,choices=(1,2),default=2)
parser.add_argument('--max-items',type=int,default=6000)
parser.add_argument('--budget',type=int,default=3600)
parser.add_argument('--symbols')
args=parser.parse_args()
if not 60<=args.budget<=7200 or not 1<=args.max_items<=6000:parser.error('finite capture bounds exceeded')
cfg=config();day=args.date;validate_collection('CN',day,cfg)
root=args.output;root.mkdir(parents=True,exist_ok=True);(root/'raw').mkdir(exist_ok=True)
with db.connect() as con:
    items=[dict(row) for row in con.execute("SELECT i.*,l.listed_date FROM instruments i LEFT JOIN instrument_lifecycle l ON l.instrument_id=i.id WHERE i.market='CN' AND i.kind='stock' AND COALESCE(l.status,'listed')='listed' ORDER BY i.id")]
    captured={row[0].split(':')[1] for row in con.execute("SELECT feed_key FROM feed_snapshots WHERE feed_key LIKE ? AND status='complete'",('intraday:CN.stock.%:'+str(day),))}
    halted={row[0] for row in con.execute("SELECT symbol FROM datasets WHERE dataset='suspensions' AND market='CN' AND trade_date=?",(str(day),))}
if args.symbols:
    requested=set(args.symbols.split(','));items=[item for item in items if item['symbol'] in requested]
    assert len(items)==len(requested),'unknown requested minute symbol'
expected=len(items)
excluded=[item for item in items if item['symbol'] in halted or (item['listed_date'] and item['listed_date']>str(day))]
candidates=[item for item in items if item['id'] not in captured and item not in excluded]
local=threading.local();began=time.monotonic();results=[]

class Source:
    def __init__(self,item):self.item=item
    def json(self,url,params):
        if not hasattr(local,'session'):
            local.session=requests.Session();local.session.trust_env=False
        reply=local.session.get(url,params=params,timeout=(5,15));reply.raise_for_status()
        if len(reply.content)>2*1024*1024:raise ValueError('oversized minute response')
        name=self.item['symbol']
        (root/'raw'/(name+'.json.gz')).write_bytes(gzip.compress(reply.content))
        (root/'raw'/(name+'.meta.json')).write_text(json.dumps(dict(url=reply.url,sha256=hashlib.sha256(reply.content).hexdigest(),observed_at=db.now_iso())),encoding='utf-8')
        time.sleep(.15)
        return reply.json()

def capture(item):
    if time.monotonic()-began>args.budget:return dict(id=item['id'],status='budget_deferred')
    try:return dict(id=item['id'],status='complete',points=collect(Source(item),item['id'],day,cfg))
    except Exception as exc:return dict(id=item['id'],status='failed',error=type(exc).__name__+': '+str(exc)[:140])

def checkpoint(phase):
    counts={}
    for result in results:counts[result['status']]=counts.get(result['status'],0)+1
    output=dict(phase=phase,target=str(day),catalog=expected,previously_captured=len(items)-len(candidates)-len(excluded),
        excluded_not_traded=len(excluded),pending_initial=len(candidates),processed=len(results),statuses=counts,
        elapsed_seconds=round(time.monotonic()-began,1),automatic_jobs_enabled=False)
    path=root/'progress.tmp';path.write_text(json.dumps(output,indent=2));path.replace(root/'progress.json');return output

with CollectorLock():
    checkpoint('collecting')
    with (root/'results.jsonl').open('a',encoding='utf-8') as log,concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks=[executor.submit(capture,item) for item in candidates[:args.max_items]]
        for task in concurrent.futures.as_completed(tasks):
            result=task.result();results.append(result);log.write(json.dumps(result)+'\n');log.flush()
            if len(results)%20==0:checkpoint('collecting')
    print(json.dumps(checkpoint('finished')))
