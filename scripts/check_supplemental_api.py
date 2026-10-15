"""Verify free supplemental APIs against retained raw responses."""
import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from marketdata import supplements as s, scheduled


def verify(base, token, day, raw_root=None):
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    checks = []
    def get(path, code=200, auth=True):
        time.sleep(.13)
        for attempt in range(3):
            try:
                response = http.open(urllib.request.Request(base.rstrip('/') + path,
                    headers={'Authorization': 'Bearer ' + token} if auth else {}), timeout=30)
            except urllib.error.HTTPError as exc:
                response = exc
            if response.status == 429 and attempt < 2:
                response.close(); time.sleep(1 + attempt); continue
            with response:
                assert response.status == code, (path, response.status)
                data = json.load(response)
            checks.append(dict(path=path, status=code))
            return data
        raise AssertionError('rate limit retries exhausted')
    def stripped(rows):
        return [{k: v for k, v in r.items() if k not in ('collected_at', 'turnover_pct', 'turnover_basis')} for r in rows]
    raw = {}
    if raw_root:
        for path in sorted(Path(raw_root).glob('*.json'), key=lambda p: p.stat().st_mtime):
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('rows') and data.get('endpoint'):
                raw[(data['endpoint'], data['kwargs'].get('symbol', data['kwargs'].get('date')))] = data['rows']
    result = dict(trade_date=day.isoformat(), checks=checks, indices={}, stocks={})
    assert get('/healthz', auth=False)['status'] == 'ok'
    for path in ['/v1/indices', '/v1/cn/limit-up-ladder', '/v1/scheduled-events?start=2026-01-01&end=2027-12-31',
                 '/v1/cn/stocks/CN.stock.000001/metrics', '/v1/cn/stocks/CN.stock.000001/minutes?trade_date=' + str(day)]:
        get(path, 401, False)
    overview = get('/v1/indices')
    assert len(overview['items']) == len(s.INDICES)
    for identifier, spec in s.INDICES.items():
        data = get('/v1/indices/' + identifier + '/bars?end=' + str(day) + '&limit=2000')
        assert data['unit'] == 'point' and data['timezone'] == str(s.ZONES[spec['market']])
        if raw:
            symbol = {'JP':'日经225指数', 'KR':'首尔综合指数'}.get(spec['market'], spec['symbol'])
            endpoint = data['source'].split('/', 1)[1]
            rejected = []
            expected = s.index_rows(raw[(endpoint, symbol)], identifier, str(day), quarantine=rejected)
            assert stripped(data['items']) == expected, identifier
            assert data['validation_report']['rejected'] == rejected
        result['indices'][identifier] = dict(rows=len(data['items']), latest=data['latest_date'],
            stale=data['stale'], source=data['source'], quarantined=data['validation_report']['quarantined_rows'])
    for symbol in ['000001','600519','300750','688981','920779']:
        identifier = 'CN.stock.' + symbol
        minute = get('/v1/cn/stocks/' + identifier + '/minutes?trade_date=' + str(day))
        value = get('/v1/cn/stocks/' + identifier + '/metrics?end=' + str(day) + '&limit=2000')
        if raw:
            assert stripped(minute['items']) == s.minute_rows(raw[('stock_zh_a_hist_min_em',symbol)], str(day))
            expected = s.valuation_rows(raw[('stock_value_em',symbol)], str(day))[-2000:]
            assert stripped(value['items']) == expected
        assert minute['items'] and value['items'] and value['latest_date'] == str(day)
        assert minute['volume_unit'] == 'lot_100_shares' and minute['adjustment'] == 'none'
        assert value['items'][-1]['turnover_pct'] is not None
        result['stocks'][symbol] = dict(minutes=len(minute['items']), metrics_returned=len(value['items']),
            latest_metrics=value['latest_date'], turnover=value['items'][-1]['turnover_pct'])
    pool = get('/v1/cn/limit-up-ladder?trade_date=' + str(day))
    assert pool['basis'] == 'provider_reported' and pool['independently_verified'] is False
    if raw:
        expected = s.pool_rows(raw[('stock_zt_pool_em',day.strftime('%Y%m%d'))], day)
        # Unlike valuation, provider turnover is part of the pool itself.
        actual = [{k:v for k,v in r.items() if k!='collected_at'} for r in pool['items']]
        assert sorted(actual,key=lambda r:r['symbol']) == expected
    result['limit_up_rows'] = pool['total']
    agenda = get('/v1/scheduled-events?start=2026-01-01&end=2027-12-31')
    assert agenda['items'] and all(r['time_precision']=='date' and r['published_at'] is None and r['occurs_at'] is None for r in agenda['items'])
    if raw_root:
        expected = scheduled.parse_fomc((Path(raw_root)/'fomc.html').read_text(encoding='utf-8'), day.year)
        known = {r['id']:r for r in agenda['items']}
        for row in expected:
            assert all(known[row['id']][k] == v for k,v in row.items())
    result['fomc_meetings'] = len(agenda['items'])
    lhb = get('/v1/cn/lhb?trade_date=' + str(day) + '&limit=1&refresh=true')
    if lhb['total'] > 1:
        following = get('/v1/cn/lhb?trade_date=' + str(day) + '&limit=500&offset=1&snapshot_id=' + lhb['snapshot_id'])
        assert following['snapshot_id'] == lhb['snapshot_id'] and following['total'] == lhb['total']
    result['lhb_total'] = lhb['total']
    assert get('/v1/us/sectors')['expected_count'] == 11
    get('/v1/cn/boards/CN.industry.THS_881121/flows',501)
    get('/v1/cn/boards/CN.industry.THS_881121/members?observed_date=' + str(day),501)
    get('/v1/indices/JP.index.unknown/bars',404)
    get('/v1/indices/CN.index.sh000001/bars?limit=2001',422)
    result['status'] = 'passed'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--date',type=date.fromisoformat,required=True)
    parser.add_argument('--raw-root',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    env=dict(line.split('=',1) for line in args.env_file.read_text(encoding='utf-8').splitlines() if '=' in line)
    result=verify(args.base,env['API_TOKEN'],args.date,args.raw_root)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(status=result['status'],checks=len(result['checks']),indices=len(result['indices']),stocks=len(result['stocks']))))


if __name__=='__main__': main()
