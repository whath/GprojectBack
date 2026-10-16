import json
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from marketdata import availability, db, funds
from marketdata.api import app
from marketdata.settings import config

HEADERS = {'Authorization': 'Bearer test-secret-token-that-is-at-least-32-characters'}


def test_capabilities_distinguishes_implementation_from_missing_production_data(monkeypatch):
    monkeypatch.setattr(availability, 'latest_ready', lambda market, cfg: date(2026, 10, 1))
    with TestClient(app) as client:
        assert client.get('/v1/capabilities').status_code == 401
        payload = client.get('/v1/capabilities', headers=HEADERS).json()
        assert payload['stock_funds']['implemented'] is True
        assert payload['data_availability']['configured_stock_funds']['current'] == 0
        assert payload['data_availability']['news']['status'] == 'not_collected'
        assert payload['data_availability']['markets']['CN']['stale'] is True
        assert client.get('/v1/cn/stocks/CN.stock.000001/flows', headers=HEADERS).status_code == 503
        assert client.get('/v1/events', headers=HEADERS).status_code == 503
        assert client.get('/v1/cn/boards/CN.industry.THS_881121/flows', headers=HEADERS).status_code == 404


def test_readiness_reports_stale_and_invalid_funds_without_masking_source_gap(monkeypatch):
    monkeypatch.setattr(availability, 'latest_ready', lambda market, cfg: date(2026, 10, 1))
    cfg = dict(config(), cn_stocks=['000001', '600519'])
    funds.save(dict(instrument_id='CN.stock.000001', currency='CNY', unit='yuan', scope='main',
                    source='akshare/test', updated_at='2026-09-30T09:00:00+00:00',
                    items=[dict(trade_date='2026-09-30', net=0, inflow=None, outflow=None)]), 'CN.stock.000001')
    with db.connect() as con:
        con.execute("INSERT INTO feed_snapshots VALUES('flows:CN.stock.600519',? ,?,'complete',NULL)",
                    (json.dumps({'bad': 'payload'}), db.now_iso()))
    result = availability.report(cfg, datetime(2026, 10, 1, tzinfo=timezone.utc))
    states = {r['instrument_id']: r for r in result['configured_stock_funds']['items']}
    assert states['CN.stock.000001']['status'] == 'stale'
    assert states['CN.stock.000001']['stored_rows'] == 1
    assert states['CN.stock.600519']['status'] == 'failed_or_invalid'
    assert result['scheduled_events_supported'] is True


def test_one_fresh_stock_cannot_hide_other_missing_series(monkeypatch):
    from marketdata import normalize
    monkeypatch.setattr(availability, 'latest_ready', lambda market, cfg: date(2026, 10, 15))
    item = dict(id='CN.stock.000001', market='CN', kind='stock', symbol='000001',
                name='sample', source_code='sz000001', currency='CNY', group_name=None)
    db.instrument(item)
    db.put_bars(item['id'], normalize.bars([{'日期':'2026-10-15','开盘':10,'最高':11,'最低':9,'收盘':10,'成交量':1}],
                                         '2026-10-15', 'stock', 'CN'))
    cfg = dict(config(), cn_all_stocks=False, cn_stocks=['000001','600519'])
    result = availability.report(cfg)
    assert result['markets']['CN']['stale'] is False
    stocks = result['configured_coverage']['CN']['categories']['stock']
    assert stocks['expected'] == 2 and stocks['available'] == 1
    assert result['configured_coverage']['CN']['data_complete'] is False
