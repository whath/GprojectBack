"""Finite ETF capture for a full-universe ranking; never enables a worker."""
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
from marketdata.history import sessions
from marketdata.pipeline import CollectorLock
from marketdata.public_feeds import store
from marketdata.settings import config
from marketdata.tencent_data import fetch_etf

parser=argparse.ArgumentParser()
parser.add_argument('--date',type=date.fromisoformat,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--workers',type=int,choices=(1,2),default=2)
parser.add_argument('--budget',type=int,default=3600)
parser.add_argument('--max-items',type=int,default=3000)
parser.add_argument('--automatic',action='store_true')
parser.add_argument('--max-attempts',type=int,choices=range(1,11))
args=parser.parse_args()
if not 60<=args.budget<=7200 or not 1<=args.max_items<=3000:parser.error('capture bound exceeded')
cfg=config();target=args.date;validate_collection('CN',target,cfg)
days=sessions('CN',target,30,cfg);required=set(days);root=args.output
root.mkdir(parents=True,exist_ok=True);(root/'raw').mkdir(exist_ok=True)
began=time.monotonic();local=threading.local()
with db.connect() as con:
    items=[dict(row) for row in con.execute("SELECT i.*,l.listed_date FROM instruments i JOIN instrument_lifecycle l ON l.instrument_id=i.id WHERE i.market='CN' AND i.kind='etf' AND l.status='listed' ORDER BY i.id")]
    actual={}
    for row in con.execute("SELECT instrument_id,trade_date FROM bars WHERE instrument_id LIKE 'CN.etf.%' AND trade_date>=? AND trade_date<=? AND adjustment='none'",(days[0],str(target))):
        actual.setdefault(row[0],set()).add(row[1])
    adjusted={}
    profiles={}
    for row in con.execute("SELECT instrument_id,source FROM bars WHERE instrument_id LIKE 'CN.etf.%' AND adjustment='none' GROUP BY instrument_id,source"):
        profiles.setdefault(row[0],set()).add(row[1])
    for row in con.execute("SELECT feed_key,payload FROM feed_snapshots WHERE feed_key LIKE 'adjusted:CN.etf.%' AND status='complete'"):
        feed=json.loads(row[1]);adjusted[row[0]]={point['trade_date'] for point in feed['items']}
assert items,'verified ETF listing catalog required'
catalog_size=len(items)
previous={}
log_path=root/'results.jsonl'
if args.max_attempts and log_path.exists():
    from marketdata.auto_batches import failed_attempts
    previous=failed_attempts(log_path)
def needed(item):
    expected={day for day in days if not item['listed_date'] or day>=item['listed_date']}
    return bool(expected-actual.get(item['id'],set()) or expected-adjusted.get('adjusted:'+item['id'],set()))
pending=[item for item in items if needed(item)]
exhausted=[item for item in pending if args.max_attempts and previous.get(item['id'],0)>=args.max_attempts]
exhausted_ids={item['id'] for item in exhausted}
items=[item for item in pending if item['id'] not in exhausted_ids]
items.sort(key=lambda item:(previous.get(item['id'],0),item['id']))
results=[]

class Source:
    def __init__(self,item,adjust):self.item=item;self.adjust=adjust
    def json(self,url,params):
        if not hasattr(local,'session'):
            local.session=requests.Session();local.session.trust_env=False
        reply=local.session.get(url,params=params,timeout=(5,15));reply.raise_for_status()
        if len(reply.content)>2*1024*1024:raise ValueError('oversized ETF response')
        payload=reply.json()
        name=self.item['symbol']+'-'+('qfq' if self.adjust else 'raw')
        (root/'raw'/(name+'.json.gz')).write_bytes(gzip.compress(reply.content))
        (root/'raw'/(name+'.meta.json')).write_text(json.dumps(dict(url=reply.url,sha256=hashlib.sha256(reply.content).hexdigest(),observed_at=db.now_iso())),encoding='utf-8')
        time.sleep(.15)
        return payload

def capture(item):
    expected={day for day in days if not item['listed_date'] or day>=item['listed_date']}
    if not expected:return dict(id=item['id'],status='not_listed_at_target')
    if time.monotonic()-began>args.budget:return dict(id=item['id'],status='budget_deferred')
    parts=[]
    for adjust,existing in [('',actual.get(item['id'],set())),('qfq',adjusted.get('adjusted:'+item['id'],set()))]:
        if expected<=existing:continue
        try:
            publisher='public/tencent-etf/v1'
            if not adjust and profiles.get(item['id'])=={'akshare/sina'}:
                from marketdata.provider import AKProvider
                from marketdata import normalize
                records=AKProvider(dict(cfg,request_attempts=1,request_timeout_seconds=35)).call('fund_etf_hist_sina',symbol=item['source_code'])
                rows=normalize.bars([row for row in records if normalize.day_string(row['日期']) in required],str(target),'etf','CN')
                publisher='akshare/sina'
                (root/'raw'/(item['symbol']+'-sina-sdk.json')).write_text(json.dumps(dict(endpoint='fund_etf_hist_sina',rows=records),ensure_ascii=False,default=str),encoding='utf-8')
            else:rows=fetch_etf(Source(item,adjust),item,target,days,adjust)
            if not rows:raise ValueError('no ETF history')
            if adjust:
                store('adjusted:'+item['id'],dict(instrument_id=item['id'],source='public/tencent-etf/qfq/v1',adjustment='qfq',
                    basis='same-provider front-adjusted closes from one bounded fetch',target_date=str(target),window=30,
                    items=[dict(trade_date=row['trade_date'],close=row['close']) for row in rows]))
            else:
                # Retain an already published series rather than mixing publishers.
                if profiles.get(item['id']) and profiles[item['id']]!={publisher}:
                    raise ValueError('ETF source change requires explicit reconciliation')
                for row in rows:row['source']=publisher
                db.put_bars(item['id'],rows)
            missing=sorted(expected-{row['trade_date'] for row in rows})
            parts.append(dict(adjustment=adjust or 'none',status='partial' if missing else 'complete',rows=len(rows),missing=missing))
        except Exception as exc:
            parts.append(dict(adjustment=adjust or 'none',status='failed',error=type(exc).__name__+': '+str(exc)[:140]))
    return dict(id=item['id'],status='complete' if all(part['status']=='complete' for part in parts) else 'partial',parts=parts)

def checkpoint(phase):
    counts={}
    for row in results:counts[row['status']]=counts.get(row['status'],0)+1
    output=dict(phase=phase,target=str(target),catalog=catalog_size,processed=len(results),statuses=counts,
        remaining_candidates=max(0,len(items)-args.max_items),retry_exhausted=len(exhausted),
        complete=phase=='finished' and not exhausted and len(items)<=args.max_items and all(row['status'] in ('complete','not_listed_at_target') for row in results),
        elapsed_seconds=round(time.monotonic()-began,1),automatic_jobs_enabled=args.automatic)
    temp=root/'progress.tmp';temp.write_text(json.dumps(output,indent=2));temp.replace(root/'progress.json')
    return output

with CollectorLock():
    checkpoint('collecting')
    with (root/'results.jsonl').open('a',encoding='utf-8') as log,concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        work=[executor.submit(capture,item) for item in items[:args.max_items]]
        for task in concurrent.futures.as_completed(work):
            row=task.result();results.append(row);log.write(json.dumps(row)+'\n');log.flush()
            if len(results)%20==0:checkpoint('collecting')
    print(json.dumps(checkpoint('finished')))
