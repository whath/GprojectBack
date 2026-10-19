from datetime import date, datetime, timedelta, timezone
import json

import pytest

from marketdata import automation as auto, db
from marketdata.settings import config


NOW=datetime(2026,9,30,8,0,tzinfo=timezone.utc)  # CN 16:00, before 17:10.


def settings(**updates):
    return dict(config(),automatic_collection=True,automatic_backfill=True,automatic_refresh_enabled=True,
                automatic_backup=False,**updates)


def job(feature='macro',generation='test'):
    return dict(job_key='CN:2026-09-30:'+generation+':'+feature,market='CN',trade_date='2026-09-30',
                feature=feature,generation=generation,payload={})


def rows():
    with db.connect() as con:return [dict(r) for r in con.execute('SELECT * FROM automation_jobs')]


def test_planner_covers_current_business_and_exact_cn_slots():
    cfg=settings()
    planned=auto.plan(cfg,NOW)
    features={row['feature'] for row in planned}
    assert {'stocks','etfs','intraday','lhb','us','catalog','macro','calendar','important-calendar','policy-news'}<=features
    assert sum(key.startswith('board-bars:') for key in features)==20
    assert sum(key.startswith('board-funds:') for key in features)==20
    assert sum(key.startswith('board-members:') for key in features)==20
    assert sum(key.startswith('ths:') for key in features)==2
    assert sum(key.startswith('index:') for key in features)==7
    assert all(row['generation']=='1510' for row in planned if row['feature'] in auto.BULK|{'lhb'})
    later=auto.plan(cfg,NOW+timedelta(hours=2))
    assert next(row for row in later if row['feature']=='lhb')['generation']=='1710'
    last=auto.plan(cfg,NOW+timedelta(hours=4))
    assert next(row for row in last if row['feature']=='lhb')['generation']=='1910'
    assert all('limit-up' not in key for key in features)


def test_before_cn_close_and_holidays_only_use_a_completed_session():
    cfg=settings()
    for now in (datetime(2026,9,30,2,tzinfo=timezone.utc),datetime(2026,10,3,8,tzinfo=timezone.utc)):
        planned=auto.plan(cfg,now)
        cn=[row for row in planned if row['feature'] in auto.BULK|{'lhb'}]
        assert all(date.fromisoformat(row['trade_date'])<now.astimezone(auto.CN).date() for row in cn)
        assert all(row['generation']=='catchup' for row in cn)
    cfg['automatic_collection']=False
    assert auto.plan(cfg,NOW)==[]


def test_planning_is_idempotent_and_does_not_redownload_completed_bulk():
    cfg=settings();auto.enqueue(cfg,NOW)
    count=len(rows())
    with db.connect() as con:con.execute("UPDATE automation_jobs SET status='complete' WHERE feature='stocks'")
    auto.enqueue(cfg,NOW)
    assert len(rows())==count
    auto.enqueue(cfg,NOW+timedelta(hours=2))
    stock=[r for r in rows() if r['feature']=='stocks']
    assert len(stock)==1 and stock[0]['status']=='complete' and stock[0]['generation']=='1710'
    assert any(r['feature']=='lhb' and r['status']=='superseded' for r in rows())


def test_later_slot_reopens_exhausted_bulk_with_a_new_attempt_group():
    cfg=settings();auto.enqueue(cfg,NOW)
    with db.connect() as con:con.execute("UPDATE automation_jobs SET status='exhausted',attempts=4 WHERE feature='etfs'")
    auto.enqueue(cfg,NOW+timedelta(hours=2))
    etf=next(r for r in rows() if r['feature']=='etfs')
    assert etf['status']=='pending' and etf['attempts']==0 and etf['generation']=='1710'


def test_bounded_retries_have_twenty_minute_delay_and_do_not_starve_other_jobs(monkeypatch):
    cfg=settings();planned=[job('macro'),job('calendar')]
    monkeypatch.setattr(auto,'plan',lambda *args:planned)
    failed=lambda job,cfg:dict(status='partial')
    result=auto.run_once(cfg,NOW,failed)
    first=result['feature'];failed_row=next(r for r in rows() if r['feature']==first)
    assert failed_row['attempts']==1 and datetime.fromisoformat(failed_row['next_at'])==NOW+timedelta(minutes=20)
    second=auto.run_once(cfg,NOW+timedelta(seconds=1),lambda j,c:dict(status='complete'))
    assert second['feature']!=first
    for index in range(1,4):auto.run_once(cfg,NOW+timedelta(minutes=20*index),failed)
    assert next(r for r in rows() if r['feature']==first)['status']=='exhausted'
    assert auto.run_once(cfg,NOW+timedelta(hours=2),failed)['status']=='idle'


def test_progress_slices_do_not_use_the_failure_retry_budget(monkeypatch):
    monkeypatch.setattr(auto,'plan',lambda *args:[job()])
    cfg=settings()
    for index in range(10):
        auto.run_once(cfg,NOW+timedelta(seconds=10*index),lambda j,c:dict(status='continuing'))
    assert rows()[0]['attempts']==0 and rows()[0]['passes']==10
    auto.run_once(cfg,NOW+timedelta(minutes=2),lambda j,c:dict(status='complete'))
    assert rows()[0]['status']=='complete'


def test_disabled_retry_does_not_queue_failed_work(monkeypatch):
    monkeypatch.setattr(auto,'plan',lambda *args:[job()])
    cfg=settings();cfg['automatic_backfill']=False
    assert auto.run_once(cfg,NOW,lambda j,c:dict(status='partial'))['status']=='exhausted'
    cfg['automatic_collection']=False
    assert auto.run_once(cfg,NOW+timedelta(hours=1))['status']=='disabled'


def test_turning_backfill_off_preserves_but_does_not_execute_an_existing_retry(monkeypatch):
    monkeypatch.setattr(auto,'plan',lambda *args:[job()])
    cfg=settings()
    assert auto.run_once(cfg,NOW,lambda j,c:dict(status='partial'))['attempts']==1
    cfg['automatic_backfill']=False
    def forbidden(*args):raise AssertionError('existing retries must respect the current switch')
    assert auto.run_once(cfg,NOW+timedelta(hours=1),forbidden)['status']=='idle'
    assert rows()[0]['attempts']==1


def test_interrupted_jobs_recover_under_lock_and_have_a_crash_limit(monkeypatch):
    monkeypatch.setattr(auto,'plan',lambda *args:[job()])
    cfg=settings();auto.enqueue(cfg,NOW)
    for index in range(4):
        with db.connect() as con:con.execute("UPDATE automation_jobs SET status='running'")
        auto.recover(cfg,NOW+timedelta(minutes=index))
    assert rows()[0]['status']=='exhausted' and rows()[0]['attempts']==4


def test_yields_before_fixed_slot_without_starting_network_work(monkeypatch):
    monkeypatch.setattr(auto,'plan',lambda *args:[job()])
    def forbidden(*args):raise AssertionError('must leave room before fixed slot')
    now=datetime(2026,9,30,9,8,tzinfo=timezone.utc)
    assert auto.run_once(settings(),now,forbidden)['status']=='yielding_for_fixed_slot'
    assert rows()[0]['passes']==0


def test_background_jobs_have_dependency_order_and_bounded_commands():
    cfg=settings();auto.enqueue(cfg,NOW)
    selected=auto.choose(NOW)
    assert selected['feature']=='catalog'
    with db.connect() as con:
        con.execute("UPDATE automation_jobs SET status='complete' WHERE feature NOT IN ('stocks','etfs','intraday')")
    chosen=auto.choose(NOW)
    assert chosen['feature'] in ('stocks','etfs')
    command=auto.command(next(r for r in rows() if r['feature']=='stocks'),cfg)
    assert '--weeks' in command and command[command.index('--weeks')+1]=='52'
    assert command[command.index('--workers')+1]=='2'
    assert command[command.index('--max-attempts')+1]=='4'
    assert '--automatic' in command and not any('TOKEN=' in arg for arg in command)


def test_worker_health_exposes_queue_without_claiming_data_complete(monkeypatch):
    from marketdata.health import health
    monkeypatch.setattr('marketdata.health.config',settings)
    auto.enqueue(settings(),NOW)
    report=health(NOW)
    assert report['automation']['counts']['pending']>0
    assert 'complete' not in report['automation']


def test_legacy_worker_honors_the_backup_off_switch(monkeypatch):
    from marketdata.scheduler import run_once
    monkeypatch.setattr('marketdata.scheduler.due_slots',lambda *args:iter([]))
    def forbidden(*args):raise AssertionError('backup was explicitly disabled')
    monkeypatch.setattr('marketdata.scheduler.daily_backup',forbidden)
    assert run_once(settings(),NOW)==[]


def test_retry_checkpoints_ignore_budget_deferrals_and_recover_torn_tail(tmp_path):
    from marketdata.auto_batches import failed_attempts
    path=tmp_path/'results.jsonl'
    rows=[dict(id='one',status='failed'),dict(id='one',status='partial'),dict(id='two',status='budget_deferred'),dict(id='three',status='complete')]
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows)+'{"id":',encoding='utf-8')
    assert failed_attempts(path)=={'one':2}
    # A resumed append must also be readable on the following pass.
    with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(dict(id='four',status='failed'))+'\n')
    assert failed_attempts(path)=={'one':2,'four':1}
    assert list(tmp_path.glob('results.jsonl.interrupted-*'))
    path.write_text('{bad}\n'+json.dumps(dict(id='four',status='failed'))+'\n',encoding='utf-8')
    with pytest.raises(ValueError,match='corrupt'):failed_attempts(path)
    path.write_text(json.dumps(dict(id='valid',status='failed')),encoding='utf-8')
    assert failed_attempts(path)=={'valid':1}
    with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(dict(id='valid',status='failed'))+'\n')
    assert failed_attempts(path)=={'valid':2}


def test_low_disk_pauses_writes_without_using_source_retry_budget(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(auto,'plan',lambda *args:[job()])
    monkeypatch.setattr(auto.shutil,'disk_usage',lambda *args:SimpleNamespace(free=100))
    def forbidden(*args):raise AssertionError('must not start new ingestion on a full disk')
    assert auto.run_once(settings(),NOW,forbidden)['status']=='paused_disk_space'
    assert rows()[0]['attempts']==rows()[0]['passes']==0


@pytest.mark.parametrize('message,expected',[('collection budget deferred; resume on bounded retry','continuing'),
                                            ('ProviderError: connection unavailable','partial')])
def test_disclosure_budget_continues_but_actual_source_failure_uses_retry_policy(monkeypatch,message,expected):
    from marketdata.pipeline import Collector
    from marketdata.normalize import PendingData
    def partial_disclosure(collector):
        collector.task('lhb',lambda:1)
        def unfinished():raise PendingData(message)
        collector.task('seats:000001:买入',unfinished)
    monkeypatch.setattr(Collector,'collect_lhb',partial_disclosure)
    task=job('lhb');task['payload']='{}';task['attempts']=0
    assert auto.execute(task,settings())['status']==expected


@pytest.mark.parametrize('modern',[True,False])
def test_worker_cli_acquires_the_global_lock_and_reloads_the_stop_switch(monkeypatch,modern):
    import sys
    import marketdata.__main__ as entry
    import marketdata.scheduler as legacy
    cfg=settings();cfg['automatic_refresh_enabled']=modern
    calls=[]
    def one_pass(current):
        calls.append(current['automatic_refresh_enabled'])
        cfg['automatic_collection']=False
    monkeypatch.setattr(entry,'config',lambda:cfg)
    monkeypatch.setattr(auto,'run_once',one_pass)
    monkeypatch.setattr(legacy,'run_once',one_pass)
    monkeypatch.setattr(entry.time,'sleep',lambda delay:None)
    monkeypatch.setattr(sys,'argv',['marketdata','worker'])
    entry.main()
    assert calls==[modern]


def test_a_new_board_or_etf_date_does_not_advance_the_stock_manifest():
    from marketdata.dashboard import report
    from marketdata.pipeline import Collector
    from marketdata import normalize
    collector=Collector('CN',date(2026,9,30))
    for kind,symbol,day in [('stock','000001','2026-09-29'),('etf','510300','2026-09-30'),('industry','BK1326','2026-09-30')]:
        item=collector.item(kind,symbol,symbol)
        db.instrument(item)
        bars=normalize.bars([dict(日期=day,开盘=10,最高=11,最低=9,收盘=10,成交量=100)],day,kind,'CN')
        db.put_bars(item['id'],bars)
    manifest=report(settings())
    assert manifest['latest_dates']['cn']=='2026-09-29'
    assert manifest['latest_dates']['boards']=='2026-09-30'
