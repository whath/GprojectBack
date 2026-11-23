import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from marketdata import db, looklook
from marketdata.api import app
from marketdata.looklook import AKSHARE_KEY, ENDPOINT, INTERVAL_SECONDS, SOURCE, TENCENT_SOURCE, refresh, snapshot, symbols
from marketdata.provider import ProviderError


class SpotProvider:
    def __init__(self, rows=None):
        self.rows = rows
        self.calls = 0

    def call(self, endpoint):
        assert endpoint == ENDPOINT
        self.calls += 1
        if self.rows is None:
            raise ProviderError('test failure')
        return self.rows


def spot(code, change, *, price=10, volume=100):
    return {'代码': code, '涨跌幅': change, '最新价': price, '成交量': volume}


class TencentSource:
    def __init__(self, quotes=None):
        self.quotes = quotes or {}
        self.batches = []

    def get(self, url, *, encoding, timeout):
        assert url.startswith('https://qt.gtimg.cn/q=') and encoding == 'gb18030' and timeout == (5, 8)
        batch = url.split('q=')[1].split(',')
        self.batches.append(batch)
        return ''.join(self.quotes.get(code, '') for code in batch)


def tencent_wire(code, stamp, change):
    values = [''] * 38
    for index, value in {2: code[2:], 3: '10', 4: '9', 5: '9', 6: '100', 30: stamp,
                         32: str(change), 33: '11', 34: '8'}.items():
        values[index] = value
    return f'v_{code}="{"~".join(values)}";'


def test_looklook_auth_and_partial_snapshot():
    source = SpotProvider([spot('300308', -3.25), spot(995, 0), spot('300502', 5, volume=0),
                           spot('300394', 'NaN'), spot('688981', 7)])
    path = '/v1/looklook/quotes'
    headers = {'Authorization': f'Bearer {os.environ["API_TOKEN"]}'}
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers).json() == {
            'updatedAt': None, 'tradeDate': None, 'source': None, 'stale': True, 'partial': True, 'quotes': {},
        }
        assert refresh({}, provider=source) == {'status': 'partial', 'count': 2, 'expected': len(symbols()),
                                                 'source': SOURCE, 'tradeDate': None}
        response = client.get(path, headers=headers)
        assert response.headers['Cache-Control'] == 'no-store'
        body = response.json()
        assert body['source'] == SOURCE and body['tradeDate'] is None
        assert body['updatedAt'] and body['partial'] and not body['stale']
        assert body['quotes'] == {'300308': {'changePct': -3.25}, '000995': {'changePct': 0}}
        client.get(path, headers=headers)
    assert source.calls == 1


def test_complete_snapshot_ages_and_survives_failed_upstream():
    source = SpotProvider([spot(code, 1.2) for code in symbols()])
    assert refresh({}, provider=source)['status'] == 'complete'
    saved = snapshot()
    assert not saved['stale'] and not saved['partial']
    assert len(saved['quotes']) == 138
    later = datetime.now(timezone.utc) + timedelta(seconds=INTERVAL_SECONDS + 90)
    assert snapshot(now=later)['stale']

    assert refresh({}, provider=SpotProvider(), tencent=TencentSource())['status'] == 'failed'
    assert snapshot() == {**saved, 'stale': True}

    with db.connect() as con:
        con.execute('DELETE FROM feed_attempts WHERE feed_key=?', (AKSHARE_KEY,))
    assert refresh({}, provider=SpotProvider([spot('300308', 2)]))['status'] == 'partial'
    current = snapshot()
    assert not current['stale'] and current['quotes'] == {'300308': {'changePct': 2}}


def test_tencent_fallback_batches_and_uses_only_newest_real_trade_date():
    source = SpotProvider([spot('300308', 1), spot('300308', 2)])
    fallback = TencentSource({
        'sz300308': tencent_wire('sz300308', '20260930150000', 2.5),
        'sz300394': tencent_wire('sz300394', '20260930150300', 0),
        'sz300502': tencent_wire('sz300502', '20260929150000', 10),
        'sz000995': tencent_wire('sz000995', '20990121150000', 20),
    })
    assert refresh({}, provider=source, tencent=fallback) == {
        'status': 'partial', 'count': 2, 'expected': len(symbols()), 'source': TENCENT_SOURCE,
        'tradeDate': '2026-09-30',
    }
    assert set(code[2:] for batch in fallback.batches for code in batch) == symbols()
    assert sorted(map(len, fallback.batches)) == [38, 50, 50]
    saved = snapshot()
    assert saved['tradeDate'] == '2026-09-30' and not saved['stale']
    assert saved['quotes'] == {
        '300308': {'changePct': 2.5, 'quotedAt': '2026-09-30T15:00:00+08:00'},
        '300394': {'changePct': 0, 'quotedAt': '2026-09-30T15:03:00+08:00'},
    }

    older = TencentSource({'sz300502': fallback.quotes['sz300502']})
    assert refresh({}, provider=source, tencent=older)['status'] == 'failed'
    assert source.calls == 1
    assert snapshot() == {**saved, 'stale': True}


def test_scheduled_refreshes_follow_shanghai_slots(monkeypatch):
    calls = []
    cfg = {'automatic_collection': True}

    def collect(settings):
        assert settings is cfg
        calls.append(now)
        return {'status': 'complete'}

    monkeypatch.setattr(looklook, 'refresh', collect)
    day = datetime(2026, 11, 23, tzinfo=looklook.CN)
    for minute in range(24 * 60):
        now = (day + timedelta(minutes=minute)).astimezone(timezone.utc)
        looklook.collect_scheduled_slot(cfg, now)
    assert [timestamp.astimezone(looklook.CN).strftime('%H:%M') for timestamp in calls] == [
        '09:26', '09:41', '09:56', '10:11', '10:26', '10:41', '10:56', '11:11', '11:26',
        '13:01', '13:16', '13:31', '13:46', '14:01', '14:16', '14:31', '14:46',
    ]


def test_scheduled_refresh_respects_calendar_and_overrides(monkeypatch):
    def unexpected_collect(_cfg):
        raise AssertionError('closed session started a collection')

    monkeypatch.setattr(looklook, 'refresh', unexpected_collect)
    cfg = {'automatic_collection': True}
    for value in ('2026-11-22T09:26:00+08:00', '2026-10-01T13:01:00+08:00'):
        assert looklook.collect_scheduled_slot(cfg, datetime.fromisoformat(value)) is None

    day = '2026-11-23'
    due = datetime.fromisoformat(f'{day}T13:01:00+08:00')
    for close in (False, '11:30'):
        override = {'calendar_overrides': {'CN': {day: close}}}
        assert looklook.collect_scheduled_slot({**cfg, **override}, due) is None
    assert looklook.collect_scheduled_slot({**cfg, 'looklook_quotes_enabled': False}, due) is None
    assert looklook.collect_scheduled_slot({}, due) is None


def test_scheduled_refresh_claims_only_current_slot_once(monkeypatch):
    calls = []

    def collect(cfg):
        calls.append(cfg)
        return {'status': 'failed'}

    monkeypatch.setattr(looklook, 'refresh', collect)
    cfg = {'automatic_collection': True}
    first = datetime(2026, 11, 23, 9, 26, tzinfo=looklook.CN)
    assert looklook.collect_scheduled_slot(cfg, first + timedelta(minutes=1)) is None
    assert looklook.collect_scheduled_slot(cfg, first + timedelta(seconds=59)) == {'status': 'failed'}
    assert looklook.collect_scheduled_slot(cfg, first + timedelta(seconds=59)) is None
    assert looklook.collect_scheduled_slot(cfg, first + timedelta(minutes=15)) == {'status': 'failed'}
    assert looklook.collect_scheduled_slot(cfg, first) is None
    assert len(calls) == 2
