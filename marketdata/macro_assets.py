"""Thirty dated observations: XAU reference, EIA Brent spot and Treasury par yields."""
import csv
import io
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup
from fastapi import HTTPException

from . import db
from .public_feeds import load, store
from .normalize import PendingData

SPECS = {
    'gold': dict(name='黄金美元参考价', source='sina/XAU/daily-reference/v1', unit='USD/troy_ounce',
                 source_page='https://finance.sina.com.cn/futures/quotes/XAU.shtml', change_unit='percent',
                 definition='publisher XAU CFD reference quote; not LBMA benchmark or exchange settlement'),
    'brent': dict(name='布伦特现货', source='official/eia/RBRTE/daily/v1', unit='USD/barrel',
                  source_page='https://www.eia.gov/dnav/pet/hist/RBRTED.htm', change_unit='percent',
                  definition='Europe Brent Spot FOB; publication lag is retained'),
    **{f'us{n}y': dict(name=f'{n}年期美债收益率', source='official/treasury/daily-par-yield/v1', unit='percent',
                      source_page='https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve',
                      change_unit='basis_points', definition=f'{n}-year daily Treasury par yield; not bond price') for n in (2, 10, 30)}
}


def finite(value):
    if isinstance(value, bool):
        raise ValueError('boolean macro observation')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite macro observation')
    return result


def recent(rows, cutoff, *, positive=True):
    seen, result = set(), []
    for row in rows:
        day = date.fromisoformat(row['data_date'])
        if day in seen:
            raise ValueError('duplicate macro date')
        seen.add(day)
        if day > cutoff:
            continue
        value = finite(row['value'])
        if (positive and value <= 0) or (not positive and not -50 < value < 100):
            raise ValueError('macro value outside valid range')
        result.append(dict(row, value=value))
    result.sort(key=lambda r: r['data_date'])
    if not result:
        raise ValueError('empty dated macro history')
    return result[-30:]


def treasury_rows(text, cutoff):
    reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not {'Date', '2 Yr', '10 Yr', '30 Yr'} <= set(reader.fieldnames or []):
        raise ValueError('Treasury CSV schema mismatch')
    rows=list(reader)
    if not rows:raise PendingData('Treasury year has no observations yet')
    results = {}
    for n in (2, 10, 30):
        points = []
        for row in rows:
            day = datetime.strptime(row['Date'], '%m/%d/%Y').date()
            raw = row[f'{n} Yr'].strip()
            if raw in ('', 'N/A'):
                continue
            points.append(dict(data_date=day.isoformat(), value=finite(raw)))
        results[f'us{n}y'] = recent(points, cutoff, positive=False)
    return results


def fetch_treasury(source,today):
    def fetch(year):
        return treasury_rows(source.get(
            f'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all',
            dict(type='daily_treasury_yield_curve',field_tdr_date_value=str(year),page='',_format='csv')),today)
    try:current=fetch(today.year)
    except PendingData:
        if today.month!=1:raise
        current={f'us{n}y':[] for n in (2,10,30)}
    if any(len(rows)<30 for rows in current.values()):
        previous=fetch(today.year-1)
        current={key:recent(previous[key]+rows,today,positive=False) for key,rows in current.items()}
    return current


def brent_rows(text, cutoff):
    soup = BeautifulSoup(text, 'html.parser')
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    if 'europe brent spot price fob' not in title:
        raise ValueError('EIA series identity mismatch')
    rows = []
    for tr in soup.select('tr'):
        cells = tr.find_all('td', recursive=False)
        if len(cells) != 6:
            continue
        label = cells[0].get_text(' ', strip=True)
        match = re.match(r'^(\d{4})\s+([A-Za-z]{3})-\s*(\d{1,2})\s+to', label)
        if not match:
            continue
        monday = datetime.strptime('-'.join(match.groups()), '%Y-%b-%d').date()
        if monday.weekday() != 0:
            raise ValueError('EIA week label is not Monday')
        for i, td in enumerate(cells[1:]):
            raw = td.get_text(' ', strip=True)
            if raw in ('', '-', '--', 'NA', 'N/A', 'W'):
                continue
            rows.append(dict(data_date=(monday+timedelta(days=i)).isoformat(), value=finite(raw)))
    return recent(rows, cutoff)


def gold_rows(text, cutoff):
    match = re.search(r'\[.*\]', text, re.S)
    if not match:
        raise ValueError('XAU daily JSONP missing array')
    rows = json.loads(match.group())
    if not isinstance(rows, list):
        raise ValueError('XAU daily array required')
    result = []
    # Old, out-of-scope archive anomalies cannot invalidate a qualified recent window.
    rows = sorted((r for r in rows if date.fromisoformat(r['date']) <= cutoff), key=lambda r:r['date'])[-30:]
    for row in rows:
        day = date.fromisoformat(row['date'])
        if day > cutoff:
            continue
        prices = {k: finite(row[k]) for k in ('open', 'high', 'low', 'close')}
        if min(prices.values()) <= 0 or prices['low'] > min(prices['open'], prices['close']) or prices['high'] < max(prices['open'], prices['close']):
            raise ValueError('invalid XAU daily OHLC')
        result.append(dict(data_date=day.isoformat(), value=prices['close']))
    return recent(result, cutoff)


def collect(source, *, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.date()
    jobs = [('treasury', lambda: fetch_treasury(source,today)),
        ('brent', lambda: {'brent': brent_rows(source.get(SPECS['brent']['source_page']), today)}),
        ('gold', lambda: {'gold': gold_rows(source.get(
            'https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_daily=/GlobalFuturesService.getGlobalFuturesDailyKLine',
            dict(symbol='XAU', source='web')), today-timedelta(days=1))})]
    results = []
    for name, action in jobs:
        try:
            series = action()
            for identifier, rows in series.items():
                store('macro:'+identifier, dict(asset_id=identifier, **SPECS[identifier], items=rows,
                      window=30, coverage='dated source observations; publication calendars differ',
                      time_precision='date', source_timezone=None if identifier=='gold' else 'America/New_York'))
            results.append(dict(feature=name, status='complete', series=list(series)))
        except Exception as exc:
            keys = ['us2y','us10y','us30y'] if name=='treasury' else [name]
            for key in keys:
                db.feed_attempt('macro:'+key, 'failed', type(exc).__name__)
            results.append(dict(feature=name, status='failed', error=type(exc).__name__))
    return results


def history(identifier, limit=30):
    if identifier not in SPECS:
        raise HTTPException(404, 'unknown macro asset')
    payload = load('macro:'+identifier)
    try:
        spec = SPECS[identifier]
        if any(payload.get(k) != v for k,v in spec.items()):
            raise ValueError('macro provenance mismatch')
        payload['items'] = recent(payload['items'], datetime.now(timezone.utc).date(), positive=identifier in ('gold','brent'))[-min(limit,30):]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, 'stored macro history failed validation') from None
    return dict(payload, latest_date=payload['items'][-1]['data_date'])


def report():
    rows = []
    for identifier in SPECS:
        try:
            payload = history(identifier)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            continue
        points = payload.pop('items')
        last = points[-1]
        change = None
        if len(points)>1:
            previous = points[-2]['value']
            change = (last['value']-previous)*100 if identifier.startswith('us') else (last['value']/previous-1)*100
        rows.append(dict(payload, **last, change=change, previous_date=points[-2]['data_date'] if len(points)>1 else None,
                         source_age_days=(datetime.now(timezone.utc).date()-date.fromisoformat(last['data_date'])).days,
                         trend=[p['value'] for p in points]))
    return dict(items=rows, configured_count=len(SPECS), available_count=len(rows), window=30)
