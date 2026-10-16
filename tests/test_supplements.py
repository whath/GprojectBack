import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from marketdata import db, supplements as s
from marketdata.api import app

HEADERS = {'Authorization': 'Bearer test-secret-token-that-is-at-least-32-characters'}


def stock():
    db.instrument(dict(id='CN.stock.000001', market='CN', kind='stock', symbol='000001', name='sample',
                       source_code='sz000001', currency='CNY'))


def minute(day='2026-10-14', clock='09:30:00', opening=0):
    return {'时间': day + ' ' + clock, '开盘': opening, '收盘': 10, '最高': 10.2, '最低': 9.9,
            '成交量': 200, '成交额': 200000, '均价': 10}


def test_minute_zero_open_is_null_no_fabricated_ohlc_and_timezone_required():
    row = s.minute_rows([minute()], '2026-10-14')[0]
    assert row['open'] is None and row['timestamp'].endswith('+08:00') and row['volume'] == 200
    for raw in [minute(clock='12:00:00'), minute(opening=11), minute(day='2026-10-13'),
                dict(minute(), 时间='2026-10-14T09:30:00+09:00')]:
        with pytest.raises(ValueError): s.minute_rows([raw], '2026-10-14')
    with pytest.raises(ValueError): s.minute_rows([minute(), minute()], '2026-10-14')


def test_japan_korea_use_own_holiday_and_close_with_delay():
    # Japan's Sports Day; China and Korea are open. Before local close, use the prior session.
    assert s.reference_close('JP', date(2026, 10, 12)) is None
    assert s.reference_close('KR', date(2026, 10, 12)) is not None
    assert s.reference_target('JP', datetime(2026, 10, 13, 5, tzinfo=timezone.utc)) == date(2026, 10, 9)


def test_reference_source_anomalies_are_quarantined_without_fabricating_holiday_bars():
    raw=[dict(date='2026-10-09',open=10,high=12,low=9,close=11),
         dict(date='2026-10-12',open=11,high=13,low=10,close=12)]
    rejected=[]
    result=s.index_rows(raw,'JP.index.N225','2026-10-13',quarantine=rejected)
    assert [r['trade_date'] for r in result]==['2026-10-09']
    assert rejected==[dict(trade_date='2026-10-12',reason='index date is not a local market session')]


def test_foreign_index_collection_is_not_blocked_by_a_china_holiday(monkeypatch):
    from marketdata.settings import config
    monkeypatch.setattr(s,'INDICES',{'JP.index.N225':s.INDICES['JP.index.N225']})
    monkeypatch.setattr(s,'validate_collection',lambda *a,**kw: (_ for _ in ()).throw(ValueError('CN closed')))
    visited=[]
    monkeypatch.setattr(s,'collect_index',lambda provider,identifier,day: visited.append(identifier) or 1)
    result=s.run(config(),date(2026,10,1),['indices'])
    assert result['status']=='complete' and visited==['JP.index.N225']


def test_source_pinning_atomic_merge_and_historical_query_never_leak_future_rows():
    key = 'index:CN.index.sh000001'
    meta = dict(source='test', instrument_id='CN.index.sh000001')
    s.save(key, meta, [dict(trade_date='2026-10-13', close=10)])
    s.save(key, meta, [dict(trade_date='2026-10-14', close=11)])
    assert len(s.saved(key)['items']) == 2
    with pytest.raises(ValueError): s.save(key, dict(meta, source='other'), [dict(trade_date='2026-10-15', close=12)])
    result = s.index_report('CN.index.sh000001', end=date(2026, 10, 13))
    assert len(result['items']) == 1 and result['latest_date'] == '2026-10-13'


def test_valuation_negative_pe_retained_and_turnover_requires_same_day_verified_volume():
    stock()
    raw = {'数据日期': '2026-10-14', '当日收盘价': 10, '总股本': 2000, '流通股本': 1000, 'PE(TTM)': -5}
    rows = s.valuation_rows([raw], '2026-10-14')
    assert rows[0]['pe_ttm'] == -5 and rows[0]['pe_static'] is None
    s.save('valuation:CN.stock.000001', dict(source='akshare/stock_value_em'), rows)
    assert s.valuation_report('CN.stock.000001', date(2026, 10, 14))['items'][0]['turnover_pct'] is None
    with db.connect() as con:
        con.execute("INSERT INTO bars VALUES('CN.stock.000001','2026-10-14','none',10,11,9,10,100,'share',1000,NULL,'akshare/tencent',?)", (db.now_iso(),))
    row = s.valuation_report('CN.stock.000001', date(2026, 10, 14))['items'][0]
    assert row['turnover_pct'] == 10 and 'dated_circulating_shares' in row['turnover_basis']['method']
    with db.connect() as con: con.execute("UPDATE bars SET volume_unit='source_unit_unverified'")
    assert s.valuation_report('CN.stock.000001', date(2026, 10, 14))['items'][0]['turnover_pct'] is None


def test_missing_supplement_is_503_auth_and_bounds_enforced():
    stock()
    with TestClient(app) as client:
        for path in ['/v1/indices', '/v1/cn/stocks/CN.stock.000001/metrics']:
            assert client.get(path).status_code == 401
        assert client.get('/v1/cn/stocks/CN.stock.000001/minutes?trade_date=2026-10-14', headers=HEADERS).status_code == 503
        assert client.get('/v1/indices/CN.index.unknown/bars', headers=HEADERS).status_code == 404
        assert client.get('/v1/cn/stocks/US.stock.NVDA/metrics', headers=HEADERS).status_code == 422
        assert client.get('/v1/cn/limit-up-ladder?trade_date=2026-10-10', headers=HEADERS).status_code == 404
        assert client.get('/v1/indices/CN.index.sh000001/bars?limit=2001', headers=HEADERS).status_code == 422


def test_provider_pool_is_explicitly_not_independent_rules_certification():
    s.save('limit-up:2026-10-14', dict(trade_date='2026-10-14', basis='provider_reported', independently_verified=False),
           [dict(symbol='000001', consecutive_days=1), dict(symbol='600519', consecutive_days=3)], 'symbol', replace=True)
    result = s.pool_report(date(2026, 10, 14))
    assert result['independently_verified'] is False and result['groups'][0]['consecutive_days'] == 3


def test_fomc_agenda_is_date_only_and_removal_does_not_mean_cancelled():
    from marketdata import scheduled
    def html(month='January', last='27-28'):
        return '<div class="panel"><div class="panel-heading">2026 FOMC Meetings</div>' + ''.join(
            f'<div class="fomc-meeting"><div class="fomc-meeting__month">{month}</div><div class="fomc-meeting__date">{last if n == 0 else str(n + 2)}</div></div>'
            for n in range(8)) + '</div>'
    rows = scheduled.parse_fomc(html(), 2026)
    assert rows[0]['occurs_at'] is None and rows[0]['published_at'] is None
    scheduled.store_fomc(rows)
    scheduled.store_fomc(scheduled.parse_fomc(html(last='29-30'), 2026))
    result = scheduled.report(date(2026, 1, 1), date(2026, 12, 31), include_removed=True)
    removed = [r for r in result['items'] if r['presence'] == 'removed_from_latest_source']
    assert len(removed) == 1 and 'cancelled' not in json.dumps(removed)
    assert len(scheduled.report(date(2026, 1, 1), date(2026, 12, 31))['items']) == 8


def test_history_inventory_does_not_skip_a_stock_with_only_the_latest_bar(monkeypatch):
    from marketdata.manual_history import catalog_coverage
    from marketdata.settings import config
    stock()
    with db.connect() as con:
        con.execute("INSERT INTO bars VALUES('CN.stock.000001','2026-10-14','none',10,11,9,10,100,'share',1000,NULL,'akshare/tencent',?)", (db.now_iso(),))
    result=catalog_coverage(date(2026,10,14),config(),250,('stock',),'catalog')
    assert result['expected']==1 and result['complete_count']==0
    assert result['items'][0]['missing_count']==249


def test_lhb_pagination_pins_disclosures_while_a_refresh_creates_a_new_version():
    with db.connect() as con:
        for n in range(3):
            con.execute("INSERT INTO datasets VALUES('lhb','CN','2026-10-14',?,NULL,?,?)",
                        (str(n),json.dumps(dict(key=str(n))),db.now_iso()))
    with TestClient(app) as client:
        url='/v1/cn/lhb?trade_date=2026-10-14&limit=2'
        first=client.get(url,headers=HEADERS).json()
        with db.connect() as con: con.execute("DELETE FROM datasets WHERE record_key='2'")
        client.get(url+'&refresh=true',headers=HEADERS)
        second=client.get(url+'&offset=2&snapshot_id='+first['snapshot_id'],headers=HEADERS).json()
        assert first['total']==second['total']==3 and second['items'][0]['key']=='2'
