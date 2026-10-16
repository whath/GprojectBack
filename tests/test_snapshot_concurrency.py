from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event

from marketdata import db
from marketdata.pipeline import Collector
from marketdata.snapshots import page


def test_snapshot_computation_does_not_hold_writer_lock_and_reads_one_database_version():
    collector=Collector('CN',date(2026,10,15))
    db.instrument(collector.item('stock','000001','first'))
    reading=Event();written=Event()
    def writer():
        assert reading.wait(2)
        db.instrument(collector.item('stock','000002','second'))
        written.set()
    def load(con):
        reading.set()
        assert written.wait(2),'ranking computation blocked an unrelated database writer'
        return dict(items=[dict(seen=con.execute('SELECT count(*) FROM instruments').fetchone()[0])],total=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(writer)
        result=page(['concurrent-ranking'],0,10,load,refresh=True)
        task.result(timeout=3)
    assert result['items']==[dict(seen=1)]  # BEGIN pins the read before the concurrent write.
    with db.connect() as con:assert con.execute('SELECT count(*) FROM instruments').fetchone()[0]==2
    cached=page(['concurrent-ranking'],0,10,lambda con: (_ for _ in ()).throw(AssertionError('unneeded rebuild')),
                snapshot_id=result['snapshot_id'])
    assert cached['items']==result['items']
