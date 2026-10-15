"""Bounded, read-only AKShare source audit; does not import or alter the market DB."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import date, timedelta

MEMBERSHIP_NAMES = ("stock_board_cons_ths", "stock_board_industry_cons_ths", "stock_board_concept_cons_ths")
ALLOWED = {"stock_fund_flow_industry", "stock_individual_fund_flow", "stock_news_em",
           "news_economic_baidu", "stock_zh_index_daily", "index_global_hist_em",
           "futures_global_spot_em", "futures_global_hist_em", "stock_zh_a_hist_min_em", "stock_individual_info_em", "stock_value_em", "stock_zt_pool_em"}


def child(request, response):
    import requests
    original_init = requests.sessions.Session.__init__
    def direct_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.trust_env = False
    requests.sessions.Session.__init__ = direct_init
    import akshare as ak
    from curl_cffi import requests as curl_requests
    def transport(url, **kwargs):
        kwargs.setdefault('timeout', 15)
        kwargs['impersonate'] = 'chrome'
        with curl_requests.Session(trust_env=False) as session:
            return session.get(url, **kwargs)
    spec = json.loads(Path(request).read_text(encoding='utf-8'))
    try:
        assert spec['endpoint'] in ALLOWED
        if spec.get('transport') == 'curl_cffi':
            requests.get = transport
        frame = getattr(ak, spec['endpoint'])(**spec['kwargs'])
        data = dict(spec, rows=json.loads(frame.to_json(orient='records', date_format='iso', force_ascii=False)))
    except Exception as exc:
        data = dict(spec, error=type(exc).__name__)
    Path(response).write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/client-source-audit'))
    parser.add_argument('--date', type=date.fromisoformat, required=True)
    parser.add_argument('--timeout', type=int, default=35)
    parser.add_argument('--only', action='append', choices=sorted(ALLOWED))
    parser.add_argument('--transport', choices=['requests', 'curl_cffi'], default='requests')
    args = parser.parse_args()
    import akshare as ak
    args.output.mkdir(parents=True, exist_ok=True)
    day = args.date.strftime('%Y%m%d')
    specs = [
        ('stock_fund_flow_industry', {'symbol': '即时'}),
        ('stock_individual_fund_flow', {'stock': '000001', 'market': 'sz'}),
        ('stock_news_em', {'symbol': '美国'}),
        ('news_economic_baidu', {'date': (args.date + timedelta(days=1)).strftime('%Y%m%d')}),
        ('stock_zh_index_daily', {'symbol': 'sh000001'}),
        ('index_global_hist_em', {'symbol': '日经225'}),
        ('index_global_hist_em', {'symbol': '韩国KOSPI'}),
        ('futures_global_spot_em', {}),
        ('futures_global_hist_em', {'symbol': 'GC00Y'}),
        ('stock_zh_a_hist_min_em', {'symbol': '000001', 'period': '1', 'adjust': '',
                                  'start_date': str(args.date) + ' 09:00:00', 'end_date': str(args.date) + ' 15:30:00'}),
        ('stock_individual_info_em', {'symbol': '000001'}),
        ('stock_value_em', {'symbol': '000001'}),
        ('stock_zt_pool_em', {'date': day}),
    ]
    result = dict(akshare_version=importlib.metadata.version('akshare'), trade_date=str(args.date),
                  membership_functions={name: hasattr(ak, name) for name in MEMBERSHIP_NAMES}, probes=[])
    print(json.dumps({k: v for k, v in result.items() if k != 'probes'}), flush=True)
    for index, (endpoint, kwargs) in enumerate(specs):
        if args.only and endpoint not in args.only:
            continue
        request, response = args.output / f'{index:02d}-request.json', args.output / f'{index:02d}-raw.json'
        transport = 'curl_cffi' if endpoint == 'stock_individual_fund_flow' else args.transport
        request.write_text(json.dumps(dict(endpoint=endpoint, kwargs=kwargs, transport=transport), ensure_ascii=False), encoding='utf-8')
        env = dict(os.environ, NO_PROXY='*', no_proxy='*')
        try:
            process = subprocess.run([sys.executable, '-m', 'scripts.probe_client_sources', '--child', str(request), str(response)],
                                     env=env, capture_output=True, timeout=args.timeout)
            data = json.loads(response.read_text(encoding='utf-8')) if process.returncode == 0 and response.exists() else {'error': 'subprocess_failed'}
        except subprocess.TimeoutExpired:
            data = {'error': 'timeout'}
        rows = data.get('rows', [])
        summary = dict(endpoint=endpoint, kwargs=kwargs, transport=transport, status='response' if rows else 'failed', count=len(rows),
                       fields=sorted({key for row in rows for key in row}), error=data.get('error'), raw_file=response.name)
        dates = [str(row[key])[:10] for row in rows for key in ('日期', '数据日期', 'date', '时间', '发布时间') if row.get(key)]
        summary['latest_date'] = max(dates, default=None)
        if endpoint == 'futures_global_spot_em':
            summary['gold_oil_candidates'] = [row for row in rows if '00Y' in str(row.get('代码', '')) and any(s in str(row.get('名称', '')) for s in ('黄金', '布伦特'))]
        result['probes'].append(summary)
        (args.output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        child(sys.argv[2], sys.argv[3])
    else:
        main()
