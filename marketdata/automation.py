"""One cloud writer, durable bounded jobs, and the current application's full data scope."""
import hashlib
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db
from .calendars import CN, CN_COLLECTION_TIMES, due_slots, latest_ready, session_close
from .operations import alert, heartbeat
from .settings import config, data_dir

log = logging.getLogger(__name__)
BULK = {'stocks', 'etfs', 'intraday'}


def init():
    with db.connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS automation_jobs(
          job_key TEXT PRIMARY KEY,market TEXT NOT NULL,trade_date TEXT NOT NULL,
          feature TEXT NOT NULL,generation TEXT NOT NULL,payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
          passes INTEGER NOT NULL DEFAULT 0,next_at TEXT NOT NULL,last_started TEXT,
          updated_at TEXT NOT NULL,result TEXT)''')
        con.execute('CREATE INDEX IF NOT EXISTS automation_due ON automation_jobs(status,next_at)')


def plan(cfg, now):
    """Calendar-based catch-up and current slots; no invented holiday sessions."""
    if not cfg.get('automatic_collection', False):
        return []
    local = now.astimezone(CN)
    slots = {market: (day, slot.rsplit(':', 1)[1]) for market, day, slot in due_slots(cfg, now)}
    for market in ('CN', 'US'):
        slots.setdefault(market, (latest_ready(market, cfg, now), 'catchup'))
    jobs = []
    def add(market, day, feature, generation, **payload):
        # Bulk checkpoints are shared by all three CN slots; completed work is retained.
        key = f'{market}:{day}:{"daily" if feature in BULK else generation}:{feature}'
        jobs.append(dict(job_key=key, market=market, trade_date=str(day), feature=feature,
                         generation=generation, payload=payload))
    cn, cn_slot = slots['CN']; us, us_slot = slots['US']
    observed = local.date()
    add('CN', cn, 'catalog', str(observed), observed=str(observed))
    add('CN', cn, 'suspensions', cn_slot)
    add('CN', cn, 'lhb', cn_slot)
    add('US', us, 'us', us_slot)
    for feature in ('stocks', 'etfs', 'intraday'):
        add('CN', cn, feature, cn_slot)
    from .focus_boards import topics
    for topic in topics():
        if topic['classification_source'] == 'ths':
            add('CN', cn, 'ths:' + topic['id'], cn_slot, topic=topic)
        else:
            for feature in ('board-bars', 'board-funds', 'board-members'):
                add('CN', cn, feature + ':' + topic['id'], cn_slot, topic=topic)
    for symbol in cfg['cn_stocks']:
        add('CN', cn, 'funds:' + symbol, cn_slot, symbol=symbol)
        add('CN', cn, 'valuation:' + symbol, cn_slot, symbol=symbol)
    # Publication feeds have wall-clock refresh periods, independent of exchange holidays.
    for feature, hours in (('policy-news', 3), ('calendar', 6), ('important-calendar', 6), ('macro', 6)):
        bucket = f'{local.date()}-{local.hour // hours * hours:02d}'
        add('CN', cn, feature, bucket)
    from .supplements import INDICES, reference_target
    for identifier, spec in INDICES.items():
        day = latest_ready('CN', cfg, now) if spec['market'] == 'CN' else reference_target(spec['market'], now)
        add('CN', day, 'index:' + identifier, 'daily', identifier=identifier)
    return jobs


def enqueue(cfg, now):
    init()
    stamp = now.isoformat()
    with db.connect() as con:
        for job in plan(cfg, now):
            payload = json.dumps(job['payload'], ensure_ascii=False)
            # New publication buckets supersede old unfinished copies, retaining their evidence.
            con.execute('''UPDATE automation_jobs SET status='superseded',updated_at=?
              WHERE market=? AND feature=? AND job_key!=? AND status IN ('pending','partial','failed')
              AND (trade_date<? OR (trade_date=? AND ? NOT IN ('stocks','etfs','intraday')))''',
              (stamp, job['market'], job['feature'], job['job_key'], job['trade_date'], job['trade_date'], job['feature']))
            old = con.execute('SELECT * FROM automation_jobs WHERE job_key=?', (job['job_key'],)).fetchone()
            if old is None:
                con.execute('''INSERT INTO automation_jobs
                  (job_key,market,trade_date,feature,generation,payload,next_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?)''', (*[job[k] for k in ('job_key','market','trade_date','feature','generation')], payload, stamp, stamp))
            elif old['generation'] != job['generation']:
                restart = old['status'] in ('failed', 'partial', 'exhausted')
                con.execute('''UPDATE automation_jobs SET generation=?,payload=?,
                  status=CASE WHEN ? THEN 'pending' ELSE status END,
                  attempts=CASE WHEN ? THEN 0 ELSE attempts END,next_at=?,updated_at=? WHERE job_key=?''',
                  (job['generation'], payload, restart, restart, stamp, stamp, job['job_key']))


def recover(cfg, now):
    """Called only under the worker OS lock; a running row belongs to a dead predecessor."""
    init()
    with db.connect() as con:
        con.execute("UPDATE automation_jobs SET status=CASE WHEN attempts+1>=? THEN 'exhausted' ELSE 'partial' END,attempts=attempts+1,next_at=?,updated_at=? WHERE status='running'",
                    (1 + cfg.get('retry_max_attempts', 3), now.isoformat(), now.isoformat()))


def summary():
    with db.connect() as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE name='automation_jobs'").fetchone()
        if not exists:
            return dict(mode='not_initialized', counts={})
        counts = dict(con.execute('SELECT status,count(*) FROM automation_jobs GROUP BY status'))
        rows = [dict(r) for r in con.execute('''SELECT market,trade_date,feature,status,attempts,passes,next_at,updated_at
            FROM automation_jobs WHERE status!='superseded' ORDER BY updated_at DESC LIMIT 12''')]
    return dict(mode='bounded_cloud_queue', counts=counts, recent=rows)


def choose(now, cfg=None):
    with db.connect() as con:
        candidates = [dict(r) for r in con.execute('''SELECT * FROM automation_jobs
          WHERE status IN ('pending','partial') AND next_at<=?
          ORDER BY COALESCE(last_started,''),CASE WHEN feature='catalog' THEN 0
            WHEN feature IN ('us','lhb','suspensions') THEN 1
            WHEN feature IN ('calendar','important-calendar','macro','policy-news') THEN 2 ELSE 3 END,job_key''', (now.isoformat(),))]
        for job in candidates:
            if job['status']=='partial' and cfg is not None and not cfg.get('automatic_backfill',False):
                continue
            feature = job['feature']
            if feature in BULK:
                catalog = con.execute("SELECT status FROM automation_jobs WHERE market='CN' AND trade_date=? AND feature='catalog' ORDER BY updated_at DESC LIMIT 1", (job['trade_date'],)).fetchone()
                if not catalog or catalog['status'] not in ('complete', 'partial', 'exhausted'):
                    continue
                suspended = con.execute("SELECT status FROM automation_jobs WHERE market='CN' AND trade_date=? AND feature='suspensions' ORDER BY updated_at DESC LIMIT 1", (job['trade_date'],)).fetchone()
                if not suspended or suspended['status'] not in ('complete', 'partial', 'exhausted'):
                    continue
            if feature == 'intraday':
                stock = con.execute("SELECT status FROM automation_jobs WHERE market='CN' AND trade_date=? AND feature='stocks'", (job['trade_date'],)).fetchone()
                if not stock or stock['status'] not in ('complete', 'exhausted'):
                    continue
            return job
    return None


def job_folder(job):
    digest = hashlib.sha256(job['job_key'].encode()).hexdigest()[:16]
    folder = data_dir() / 'automation' / job['trade_date'] / digest / job['generation'].replace(':', '-')
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def command(job, cfg):
    folder = job_folder(job); feature = job['feature']
    if feature in BULK:
        script = {'stocks':'collect_all_a.py','etfs':'collect_all_etfs.py','intraday':'collect_all_minutes.py'}[feature]
        args = [sys.executable, str(Path(__file__).resolve().parents[1]/'scripts'/script),
                '--date', job['trade_date'], '--output', str(folder), '--workers', '2',
                '--budget', '90', '--max-items', str(max(1,min(200,int(cfg.get('automatic_batch_items', 120))))),
                '--automatic', '--max-attempts', str(1 + cfg.get('retry_max_attempts', 3))]
        if feature == 'stocks':
            args += ['--weeks','52','--attempt-group', job['generation']]
        return args
    return [sys.executable, '-m', 'marketdata.automation', '--execute', job['job_key']]


def process_job(job, cfg):
    folder = job_folder(job)
    result_path = folder/'result.json'
    result_path.unlink(missing_ok=True)
    limit = max(60, min(240, int(cfg.get('automatic_job_timeout_seconds', 180))))
    with (folder/'worker.log').open('ab') as output:
        process = subprocess.Popen(command(job, cfg), stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=os.name != 'nt')
        began = time.monotonic(); beat = began
        while process.poll() is None:
            now = time.monotonic()
            if now - beat >= 15:
                heartbeat('automatic:' + job['feature']); beat = now
            if now - began > limit:
                if os.name != 'nt': os.killpg(process.pid, signal.SIGTERM)
                else: process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name != 'nt': os.killpg(process.pid, signal.SIGKILL)
                    else: process.kill()
                    process.wait()
                return dict(status='partial', reason='job_timeout; saved checkpoints retained')
            time.sleep(1)
    if job['feature'] in BULK:
        progress = folder/'progress.json'
        if not progress.exists():
            return dict(status='partial', reason='bulk_job_failed_before_checkpoint', exit_code=process.returncode)
        result = json.loads(progress.read_text())
        if process.returncode not in (0, 2):
            return dict(status='partial', reason='bulk_job_failed', exit_code=process.returncode)
        if result.get('complete'):
            return dict(status='complete', progress=result)
        # Unvisited instruments do not consume the upstream failure retry allowance.
        deferred = result.get('remaining_candidates', 0) > 0 or result.get('statuses', {}).get('budget_deferred', 0) > 0
        return dict(status='continuing' if deferred else 'partial', progress=result)
    if not result_path.exists():
        return dict(status='partial', reason='job_failed_before_result', exit_code=process.returncode)
    return json.loads(result_path.read_text())


def run_once(cfg, now=None, runner=process_job):
    simulated = now is not None
    now = now or datetime.now(timezone.utc)
    if not cfg.get('automatic_collection', False):
        return dict(status='disabled')
    heartbeat('automatic:planning')
    enqueue(cfg, now)
    if shutil.disk_usage(data_dir()).free < 1024**3:
        alert('automatic:disk_space','Collection paused with less than 1 GiB free; stored data retained','error')
        heartbeat('automatic:disk_paused')
        return dict(status='paused_disk_space')
    alert('automatic:disk_space')
    local = now.astimezone(CN)
    if session_close('CN', local.date(), cfg) is not None and any(
        timedelta(0) < datetime.combine(local.date(), at, CN)-local < timedelta(minutes=5) for at in CN_COLLECTION_TIMES):
        heartbeat('idle'); return dict(status='yielding_for_fixed_slot')
    job = choose(now,cfg)
    if job is None:
        heartbeat('idle'); return dict(status='idle')
    with db.connect() as con:
        con.execute("UPDATE automation_jobs SET status='running',last_started=?,updated_at=? WHERE job_key=?",
                    (now.isoformat(), now.isoformat(), job['job_key']))
    heartbeat('automatic:' + job['feature'])
    try:
        result = runner(job, cfg)
    except Exception as exc:
        log.exception('automatic job failed: %s', job['feature'])
        result = dict(status='partial', reason=type(exc).__name__)
    ended = now if simulated else datetime.now(timezone.utc)
    status = result.get('status')
    attempts = job['attempts']
    if status == 'complete':
        next_at = ended
    elif status == 'continuing':
        status = 'pending'; next_at = ended + timedelta(seconds=5)
    else:
        attempts += 1
        status = 'partial' if cfg.get('automatic_backfill', False) and attempts < 1 + cfg.get('retry_max_attempts', 3) else 'exhausted'
        next_at = ended + timedelta(minutes=cfg.get('retry_interval_minutes', 20))
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    with db.connect() as con:
        con.execute('''UPDATE automation_jobs SET status=?,attempts=?,passes=passes+1,
          next_at=?,updated_at=?,result=? WHERE job_key=?''',
          (status, attempts, next_at.isoformat(), ended.isoformat(), encoded, job['job_key']))
    alert('automatic:' + job['feature'], None if status == 'complete' else
          f'{job["trade_date"]}: {status}; partial data retained; retries are bounded', 'warning')
    heartbeat('idle')
    log.info('automatic feature=%s date=%s status=%s attempts=%s', job['feature'], job['trade_date'], status, attempts)
    return dict(feature=job['feature'], trade_date=job['trade_date'], status=status, attempts=attempts)


def execute(job, cfg):
    """Child-process adapter dispatch; no credentials in argv or generated artifacts."""
    from .pipeline import Collector, CollectorLock
    from .public_feeds import PublicSource
    day = date.fromisoformat(job['trade_date']); feature = job['feature']
    payload = json.loads(job['payload']); folder = job_folder(job)
    source = PublicSource(folder/'raw')
    cfg = dict(cfg, request_attempts=1, request_timeout_seconds=25, collection_budget_seconds=150,
               raw_response_dir=str(folder/'raw'))
    if feature == 'us':
        result=Collector('US', day, settings=cfg, retry_only=job['attempts']>0).run()
        from .operations import report_tasks
        unfinished=[r for r in report_tasks('US',str(day)) if r['status']!='complete']
        if unfinished and all(r['reason_code']=='budget_deferred' for r in unfinished):
            result['status']='continuing'
        return result
    if feature.startswith('valuation:'):
        from .supplements import run
        return run(cfg, day, ['valuation'], [payload['symbol']], folder/'raw')
    with CollectorLock():
        results = []
        if feature == 'catalog':
            from .lifecycle import ROUTES, publish
            from .provider import AKProvider
            provider = AKProvider(cfg)
            for endpoint, kwargs, code, name, delist in ROUTES:
                try:
                    records = provider.call(endpoint, **kwargs)
                    count = publish(records, endpoint, code, name, delist, payload['observed'], endpoint+':'+str(kwargs.get('symbol','')))
                    results.append(dict(feature=endpoint, status='complete', rows=count))
                except Exception as exc:
                    results.append(dict(feature=endpoint, status='failed', reason=type(exc).__name__))
            rows = provider.call('fund_etf_category_sina', symbol='ETF基金')
            if not 1000 < len(rows) < 10000: raise ValueError('ETF catalog outside validated size bounds')
            items = []
            for row in rows:
                symbol = str(row['代码']).zfill(6)
                items.append(dict(id='CN.etf.'+symbol, market='CN', kind='etf', symbol=symbol,
                    name=str(row['名称']), source_code=row.get('source_code',('sh' if symbol.startswith(('5','6')) else 'sz')+symbol),currency='CNY'))
            db.publish_catalog(items,'CN','etf',payload['observed'],'akshare/fund_etf_category_sina',full=True)
            from .lifecycle import report
            if not report()['directory_complete']: results.append(dict(feature='stock-directory',status='failed'))
        elif feature == 'suspensions':
            from .history import sessions
            from .provider import AKProvider
            provider=AKProvider(cfg)
            for target in sessions('CN',day,5,cfg):
                key='suspensions:'+target
                try:
                    records=provider.call('cn_suspensions',day=target)
                    if records:db.put_dataset('suspensions','CN',target,records)
                    else:
                        with db.connect() as con:con.execute("DELETE FROM datasets WHERE dataset='suspensions' AND trade_date=?",(target,))
                    db.feed_attempt(key,'complete')
                    results.append(dict(feature=key,status='complete',rows=len(records)))
                except Exception as exc:
                    db.feed_attempt(key,'failed',type(exc).__name__)
                    results.append(dict(feature=key,status='failed',reason=type(exc).__name__))
        elif feature == 'lhb':
            collector=Collector('CN',day,settings=cfg);collector.collect_lhb()
            from .operations import report_tasks
            results=[r for r in report_tasks('CN',str(day)) if r['task_key'].startswith(('lhb','seats:'))]
        elif feature.startswith(('board-', 'ths:')):
            from . import board_data, selected_ths
            topic=payload['topic']; item=dict(id=topic['id'],market='CN',kind=topic['kind'],symbol=topic['symbol'],
                name=topic['source_name'],source_code=topic['symbol'],currency='CNY',group_name=topic['classification_source'])
            db.instrument(item)
            if feature.startswith('ths:'): selected_ths.collect(source,topic,day,cfg)
            elif feature.startswith('board-bars:'): Collector('CN',day,settings=cfg,refresh=True).history(item,'stock_board_'+topic['kind']+'_hist_em')
            elif feature.startswith('board-funds:'): board_data.collect_flow(source,item,day,cfg)
            else:
                # Membership is an observation of today's constituents, never a reconstructed historical set.
                db.put_dataset('members:'+item['id'],'CN',datetime.now(CN).date().isoformat(),board_data.current_members(source,topic['symbol']))
            results=[dict(feature=feature,status='complete')]
        elif feature.startswith('funds:'):
            from .funds import collect_stock
            with db.connect() as con:
                item=con.execute("SELECT * FROM instruments WHERE id=?",('CN.stock.'+payload['symbol'],)).fetchone()
            if item is None: raise ValueError('configured stock identity missing')
            collect_stock(Collector('CN',day,settings=dict(cfg,akshare_transport=cfg.get('funds_transport','curl_cffi'))),dict(item))
            results=[dict(feature=feature,status='complete')]
        elif feature.startswith('index:'):
            from .supplements import collect_index,INDICES
            from .provider import AKProvider
            identifier=payload['identifier']
            provider=AKProvider(dict(cfg,**({} if INDICES[identifier]['market']=='CN' else {'akshare_transport':'curl_cffi'})))
            collect_index(provider,identifier,day);results=[dict(feature=feature,status='complete')]
        elif feature == 'macro':
            from .macro_assets import collect
            results=collect(source)
        elif feature == 'calendar':
            from .calendar_feed import collect
            results=[collect(source,cfg)]
        elif feature == 'important-calendar':
            from .important_calendar import collect
            results=collect(source,cfg)
        elif feature == 'policy-news':
            from .policy_news import collect
            from .priority_news import collect as priority
            results=collect(source,cfg)+priority(source,cfg)
        else:
            raise ValueError('unsupported automatic feature')
        unfinished=[r for r in results if r['status']!='complete']
        status='complete' if results and not unfinished else 'partial'
        if feature=='lhb' and unfinished and all(r.get('reason_code')=='budget_deferred' for r in unfinished):
            status='continuing'
        return dict(status=status, items=results)


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--execute',required=True)
    args=parser.parse_args()
    with db.connect() as con:
        row=con.execute('SELECT * FROM automation_jobs WHERE job_key=?',(args.execute,)).fetchone()
    if row is None: raise SystemExit('Unknown scheduled job')
    job=dict(row)
    result=execute(job,config())
    (job_folder(job)/'result.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False),encoding='utf-8')


if __name__=='__main__':
    main()
