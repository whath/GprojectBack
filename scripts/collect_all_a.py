"""One finite, resumable, rate-bounded full A-share capture; no scheduler."""
import argparse,concurrent.futures,gzip,hashlib,importlib,json,os,sys,threading,time
from types import SimpleNamespace
from datetime import date,datetime,timezone
from pathlib import Path
import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdata import db,normalize
from marketdata.calendars import validate_collection
from marketdata.history import sessions,week_sessions
from marketdata.pipeline import CollectorLock
from marketdata.provider import adapt_frame
from marketdata.public_feeds import store
from marketdata.settings import config
import akshare as ak

parser=argparse.ArgumentParser(description='Finite, resumable A-share collection; never enables a scheduler')
parser.add_argument('--date',type=date.fromisoformat,required=True)
parser.add_argument('--output',type=Path)
parser.add_argument('--workers',type=int,choices=[1,2,3,4],default=2)
parser.add_argument('--budget',type=int,default=7200)
parser.add_argument('--max-items',type=int,default=10000)
parser.add_argument('--plan-only',action='store_true')
parser.add_argument('--weeks',type=int,choices=[52],help='52 calendar weeks; omission retains the 30-session mode')
parser.add_argument('--symbols',help='Optional comma-separated six-digit symbols for a finite canary')
parser.add_argument('--automatic',action='store_true',help='Run as a checkpointed cloud queue slice')
parser.add_argument('--max-attempts',type=int,choices=range(1,11))
parser.add_argument('--attempt-group',choices=['daily','catchup','1510','1710','1910'],default='daily')
args=parser.parse_args()
if not 60<=args.budget<=14400 or not 1<=args.max_items<=10000:parser.error('budget must be 60..14400 and max-items 1..10000')
cfg=config();target=args.date;days=week_sessions('CN',target,args.weeks,cfg) if args.weeks else sessions('CN',target,30,cfg);dayset=set(days)
scope='all_a_52w_manual' if args.weeks else 'all_a_30_manual'
if args.automatic:scope=('all_a_52w_auto_' if args.weeks else 'all_a_30_auto_')+args.attempt_group
validate_collection('CN',target,cfg);started=time.monotonic();max_seconds=args.budget
from marketdata.lifecycle import report as directory_report
directory=directory_report()
if not directory['directory_complete']:raise SystemExit('Refresh and verify all four exchange catalogs before full-universe collection')
root=args.output or Path('data')/('all-a-'+str(target));root.mkdir(parents=True,exist_ok=True)
rawdir=root/'raw';rawdir.mkdir(exist_ok=True)
module=importlib.import_module(ak.stock_zh_a_hist_tx.__module__)
# The verified exchange catalog already supplies listing metadata. This helper
# only clamps request start; avoiding its redundant remote discovery does not
# change prices or infer a listing date.
module.get_tx_start_year=lambda symbol:days[0]
module.get_tqdm=lambda:lambda iterable,**kwargs:iterable
# The transport below already validates every response as strict JSON. Values
# match AKShare's generic JS parser (recorded parser-check.json); avoid parsing
# the same strict payload a second time with a much slower permissive decoder.
module.demjson=SimpleNamespace(decode=json.loads)
local=threading.local();counter_lock=threading.Lock();http_count=0
def pooled_get(url,**kwargs):
    global http_count
    if not hasattr(local,'session'):
        local.session=requests.Session();local.session.trust_env=False
    # AKShare requests a whole year by default. The same publisher endpoint,
    # adjustment flag and parser can serve a bounded recent window instead.
    if '/newfqkline/get' in url:
        params=dict(kwargs.get('params',{}));values=params['param'].split(',')
        params['param']=','.join([values[0],'day',days[0],str(target),str(len(days)+10),values[-1]])
        kwargs['params']=params
        # SDK loops over two years. Identical bounded parameters within this
        # one fetch must reuse the same response, including qfq vintage.
        key=params['param']
        if getattr(local,'last_key',None)==key:return local.last_reply
    kwargs['timeout']=(5,15)
    reply=local.session.get(url,**kwargs);reply.raise_for_status()
    if '/newfqkline/get' in url:
        import re
        text=reply.text;pos=text.find('={');payload=json.loads(text[pos+1:] if pos>=0 else text)
        symbol=kwargs['params']['param'].split(',')[0]
        if payload.get('code')!=0 or symbol not in payload.get('data',{}):raise ValueError('Tencent response identity mismatch')
        local.last_key=key;local.last_reply=reply
    with counter_lock:http_count+=1
    time.sleep(.12)
    return reply
requests.get=pooled_get

with db.connect() as con:
    items=[dict(row) for row in con.execute("SELECT i.*,l.listed_date FROM instruments i LEFT JOIN instrument_lifecycle l ON i.id=l.instrument_id WHERE i.market='CN' AND i.kind='stock' AND COALESCE(l.status,'listed')='listed' ORDER BY i.id")]
    actual={};profiles={}
    for row in con.execute("SELECT instrument_id,trade_date,source FROM bars WHERE instrument_id LIKE 'CN.stock.%' AND adjustment='none' AND trade_date>=? AND trade_date<=?",(days[0],str(target))):
        actual.setdefault(row[0],set()).add(row[1]);profiles.setdefault(row[0],set()).add(row[2])
    for identifier,publisher in con.execute("SELECT DISTINCT instrument_id,source FROM bars WHERE instrument_id LIKE 'CN.stock.%' AND adjustment='none'"):
        profiles.setdefault(identifier,set()).add(publisher)
    adjusted={}
    for row in con.execute("SELECT feed_key,payload FROM feed_snapshots WHERE feed_key LIKE 'adjusted:CN.stock.%' AND status='complete'"):
        feed=json.loads(row[1]);adjusted[row[0]]={'dates':{r['trade_date'] for r in feed.get('items',[])},'target_date':feed.get('target_date')}
    previous={row[0]:row[1] for row in con.execute("SELECT task_key,attempts FROM tasks WHERE market='CN' AND trade_date=? AND scope=?",(str(target),scope))}
    suspended={}
    for symbol,day in con.execute("SELECT symbol,trade_date FROM datasets WHERE dataset='suspensions' AND market='CN' AND trade_date>=? AND trade_date<=?",(days[0],str(target))):
        suspended.setdefault(symbol,set()).add(day)

def needs_capture(item):
    required={d for d in days if not item['listed_date'] or d>=item['listed_date']}-suspended.get(item['symbol'],set())
    return bool(required-actual.get(item['id'],set()) or required-adjusted.get('adjusted:'+item['id'],{}).get('dates',set()))

universe_size=len(items)
if args.symbols:
    requested=set(args.symbols.split(','))
    if not all(len(s)==6 and s.isdigit() for s in requested):parser.error('symbols must be six-digit codes')
    items=[item for item in items if item['symbol'] in requested]
    if len(items)!=len(requested):parser.error('symbol is absent from the verified current A-share catalog')
    universe_size=len(items)
candidates=[item for item in items if needs_capture(item)]
exhausted=[item for item in candidates if args.max_attempts and max(previous.get('bar:'+item['id'],0),previous.get('adjusted:'+item['id'],0))>=args.max_attempts]
exhausted_ids={item['id'] for item in exhausted}
candidates=[item for item in candidates if item['id'] not in exhausted_ids]
candidates.sort(key=lambda item:(max(previous.get('bar:'+item['id'],0),previous.get('adjusted:'+item['id'],0)),item['id']))
pending_ids={item['id'] for item in candidates}
already_complete=universe_size-len(candidates)-len(exhausted)
if args.plan_only:
    print(json.dumps({'target':str(target),'first':days[0],'window_weeks':args.weeks,'window_sessions':len(days),'catalog':universe_size,'already_accounted':already_complete,'pending':len(candidates),'batch_limit':args.max_items}));raise SystemExit(0)
items=candidates[:args.max_items]

results=[]
def evidence(item,adjust,endpoint,kwargs,rows):
    payload={'instrument_id':item['id'],'endpoint':endpoint,'kwargs':kwargs,'rows':rows,'captured_at':db.now_iso(),
             'window_sessions':len(days),'window_weeks':args.weeks,'transport':'pooled bounded same-publisher AKShare parser'}
    encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
    file=rawdir/(item['symbol']+'-'+('qfq' if adjust else 'raw')+'.json.gz')
    file.write_bytes(gzip.compress(encoded));return hashlib.sha256(encoded).hexdigest()

def capture(item):
    identifier=item['id'];expected={d for d in days if not item['listed_date'] or d>=item['listed_date']}-suspended.get(item['symbol'],set())
    if not expected:return {'id':identifier,'status':'not_yet_listed_at_target','listed_date':item['listed_date']}
    if time.monotonic()-started>max_seconds:return {'id':identifier,'status':'budget_deferred'}
    key='adjusted:'+identifier;feed=adjusted.get(key,{})
    need_raw=bool(expected-actual.get(identifier,set()))
    need_qfq=feed.get('target_date')!=str(target) or bool(expected-feed.get('dates',set()))
    if not need_raw and not need_qfq:return {'id':identifier,'status':'already_complete','raw_rows':len(expected)}
    outcomes=[]
    for adjust,needed in [('',need_raw),('qfq',need_qfq)]:
        if not needed:continue
        task_key=('adjusted:' if adjust else 'bar:')+identifier;begin=time.monotonic()
        try:
            # Retain the one pre-existing Beijing Sina series; new symbols
            # use the validated Tencent route supported by this AKShare build.
            sina=profiles.get(identifier)=={'akshare/sina'}
            endpoint='stock_zh_a_daily' if sina else 'stock_zh_a_hist_tx'
            kwargs={'symbol':item['source_code'],'start_date':days[0].replace('-',''),'end_date':str(target).replace('-',''),'adjust':adjust}
            if endpoint.endswith('_tx'):kwargs['timeout']=15
            frame=getattr(ak,endpoint)(**kwargs);frame=adapt_frame(endpoint,frame,kwargs)
            raw=json.loads(frame.to_json(orient='records',date_format='iso',force_ascii=False))
            raw=[r for r in raw if str(r['日期'])[:10] in dayset]
            rows=normalize.bars(raw,str(target),'stock','CN')
            digest=evidence(item,adjust,endpoint,kwargs,raw)
            if not rows:raise normalize.PendingData('no observations in requested session window')
            if adjust:
                store(key,dict(instrument_id=identifier,source='akshare/'+endpoint+'/qfq/v1',adjustment='qfq',
                    basis='same-provider front-adjusted closes from one fetch',target_date=str(target),window=len(days),window_weeks=args.weeks,
                    items=[{'trade_date':r['trade_date'],'close':r['close']} for r in rows]))
            else:
                for row in rows:row['source']='akshare/sina' if sina else 'akshare/tencent'
                db.put_bars(identifier,rows)
            missing=sorted(expected-{r['trade_date'] for r in rows})
            status='pending' if missing else 'complete'
            outcomes.append({'part':'qfq' if adjust else 'raw','status':status,'rows':len(rows),'missing':missing,'source':endpoint,'sha256':digest})
            message='missing sessions require suspension evidence' if missing else None
        except Exception as exc:
            status='failed';message=type(exc).__name__+': '+str(exc)[:120]
            if adjust:db.feed_attempt(key,'failed',type(exc).__name__)
            outcomes.append({'part':'qfq' if adjust else 'raw','status':status,'error':message})
        with db.connect() as con:
            con.execute("INSERT INTO tasks(market,trade_date,scope,task_key,status,row_count,attempts,message,updated_at) VALUES('CN',?,?,?,?,?,1,?,?) ON CONFLICT(market,trade_date,scope,task_key) DO UPDATE SET status=excluded.status,row_count=excluded.row_count,attempts=attempts+1,message=excluded.message,updated_at=excluded.updated_at",
                        (str(target),scope,task_key,status,outcomes[-1].get('rows',0),message,db.now_iso()))
    return {'id':identifier,'status':'complete' if all(o['status']=='complete' for o in outcomes) else 'partial','parts':outcomes,'seconds':round(time.monotonic()-begin,3)}

def checkpoint():
    statuses={}
    for result in results:statuses[result['status']]=statuses.get(result['status'],0)+1
    result={'phase':'collecting','target':str(target),'first':days[0],'window_weeks':args.weeks,'window_sessions':len(days),'expected':universe_size,'batch_size':len(items),'already_accounted':already_complete,'processed':len(results),'statuses':statuses,'http_requests':http_count,
            'elapsed_seconds':round(time.monotonic()-started,1),'updated_at':db.now_iso(),'workers':args.workers,'automatic_jobs_enabled':args.automatic,
            'remaining_candidates':max(0,len(candidates)-len(items)),'retry_exhausted':len(exhausted)}
    temp=root/'progress.tmp';temp.write_text(json.dumps(result,ensure_ascii=False,indent=2));temp.replace(root/'progress.json')
    return result

with CollectorLock():
    checkpoint()
    with (root/'results.jsonl').open('a',encoding='utf-8') as log,concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        pending={executor.submit(capture,item):item['id'] for item in items}
        for future in concurrent.futures.as_completed(pending):
            try:result=future.result()
            except Exception as exc:result={'id':pending[future],'status':'internal_error','error':type(exc).__name__+': '+str(exc)[:160]}
            results.append(result);log.write(json.dumps(result,ensure_ascii=False)+'\n');log.flush()
            if len(results)%20==0:checkpoint()
    summary=checkpoint();summary['phase']='finished';summary['complete']=not exhausted and len(candidates)<=args.max_items and all(r['status'] in ['complete','already_complete','not_yet_listed_at_target'] for r in results)
    final=root/'progress.tmp';final.write_text(json.dumps(summary,ensure_ascii=False,indent=2));final.replace(root/'progress.json');print(json.dumps(summary,ensure_ascii=False))
    raise SystemExit(0 if summary['complete'] else 2)
