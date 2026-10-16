"""Important CN/US meetings and data releases, with publisher-confirmed dates only.

No HTTP read collects upstream data. Failed refreshes retain last verified schedules.
Official sources are kept separate from the WSCN high-importance fallback.
"""
import calendar
import ast
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from fastapi import HTTPException

from . import db
from .public_feeds import load, store
from .scheduled import URL as FED_URL, parse_fomc

NBS_URL = 'https://www.stats.gov.cn/xw/tjxw/tzgg/202512/t20251224_1962137.html'
BEA_URL = 'https://www.bea.gov/news/schedule'
BLS_URL = 'https://www.bls.gov/schedule/{year}/{month:02d}_sched_list.htm'
CN_LIST = 'https://www.mofcom.gov.cn/xwfb/ldrhd/index.html'
# Verified official announcement, not an inferred annual date. New announcements
# can be supplied as cfg.important_meeting_urls and discovered on the official list.
CN_SEEDS = ['https://www.mofcom.gov.cn/xwfb/ldrhd/art/2026/art_aa4f16caa58f4339b8ef9cdf6406c372.html']
SOURCES = ('nbs', 'bea', 'bls', 'fed', 'cn-meetings')
NBS_IMPORTANT = {
    '国民经济运行情况': '国民经济运行（含季度 GDP、工业、消费与投资）',
    '采购经理指数月度报告': '中国官方 PMI',
    '居民消费价格指数月度报告': '中国 CPI',
    '工业生产者价格指数月度报告': '中国 PPI',
}


def event(identifier, title, country, category, start, end, url, issuer, source, *, stamp=None, zone=None):
    return dict(id=identifier, title=title, country_code=country, category=category,
                start_date=str(start), end_date=str(end), occurs_at=stamp.astimezone(timezone.utc).isoformat() if stamp else None,
                local_date=str(stamp.astimezone(ZoneInfo('Asia/Shanghai')).date()) if stamp else None,
                time_precision='minute' if stamp else 'date', timezone=zone or ('Asia/Shanghai' if country == 'CN' else 'America/New_York'),
                importance=4, source=source, authority='official', issuer=issuer, source_page=url,
                published_at=None, schedule_status='announced', note='计划日期，可能由发布机关调整')


def table_grid(table):
    """Expand rowspan/colspan so a blank February never shifts March's date/time."""
    grid, occupied = [], {}
    for r, row in enumerate(table.find_all('tr')):
        cells = {}
        for (rr, cc), text in occupied.items():
            if rr == r:
                cells[cc] = text
        c = 0
        for cell in row.find_all(['th', 'td'], recursive=False):
            while c in cells:
                c += 1
            text = re.sub(r'\s+', ' ', cell.get_text(' ', strip=True))
            for dr in range(int(cell.get('rowspan', 1))):
                for dc in range(int(cell.get('colspan', 1))):
                    occupied[(r + dr, c + dc)] = text
                    if dr == 0:
                        cells[c + dc] = text
            c += int(cell.get('colspan', 1))
        grid.append([cells.get(n, '') for n in range(max(cells, default=-1) + 1)])
    return grid


def parse_nbs(html, year, url=NBS_URL):
    soup = BeautifulSoup(html, 'html.parser')
    if not re.search(rf'{year}年国家统计局主要统计信息发布日程表', soup.get_text('', strip=True)):
        raise ValueError('NBS annual schedule year mismatch')
    table = next((t for t in soup.select('table') if '居民消费价格指数月度报告' in t.get_text()), None)
    if table is None:
        raise ValueError('NBS schedule table missing')
    grid, rows = table_grid(table), []
    for i, cells in enumerate(grid):
        if len(cells) != 14 or cells[1] not in NBS_IMPORTANT or i + 1 >= len(grid):
            continue
        # A date row contains day/weekday; the next row contains the time.
        if not any(re.search(r'\d{1,2}/[一二三四五六日]', v) for v in cells[2:]):
            continue
        for month, text in enumerate(cells[2:], 1):
            days = re.findall(r'(\d{1,2})/[一二三四五六日]', text)
            if not days:
                continue
            clock = re.fullmatch(r'(\d{1,2}):(\d{2})', grid[i + 1][month + 1])
            if not clock:
                raise ValueError('NBS explicit release time missing')
            for day in days:
                stamp = datetime(year, month, int(day), int(clock[1]), int(clock[2]), tzinfo=ZoneInfo('Asia/Shanghai'))
                key = list(NBS_IMPORTANT).index(cells[1])
                rows.append(event(f'nbs-{key}-{stamp.date()}', NBS_IMPORTANT[cells[1]], 'CN', 'release', stamp.date(), stamp.date(),
                                  url, '国家统计局', 'official/nbs/release-calendar/v1', stamp=stamp))
    if len(rows) < 40 or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('NBS important schedule incomplete or duplicate')
    return rows


def parse_bea(html):
    soup, rows, year = BeautifulSoup(html, 'html.parser'), [], None
    for tr in soup.select('table tr'):
        cells = [c.get_text(' ', strip=True) for c in tr.find_all(['td', 'th'], recursive=False)]
        if not cells:
            continue
        heading = re.search(r'Year (20\d{2})', cells[0])
        if heading:
            year = int(heading[1])
            continue
        if len(cells) < 3 or year is None:
            continue
        title = cells[-1]
        if not (title.startswith('GDP (') or title.startswith('Personal Income and Outlays,')):
            continue
        clock = re.fullmatch(r'([A-Za-z]+ \d{1,2})\s+(\d{1,2}:\d{2} [AP]M)', cells[0])
        if not clock:
            raise ValueError('BEA important release lacks explicit date/time')
        stamp = datetime.strptime(f'{year} {clock[1]} {clock[2]}', '%Y %B %d %I:%M %p').replace(tzinfo=ZoneInfo('America/New_York'))
        is_gdp = title.startswith('GDP (')
        label = ('美国 GDP：' + title) if is_gdp else ('美国 PCE / 个人收入与支出：' + title)
        rows.append(event(f'bea-{"gdp" if is_gdp else "pce"}-{stamp.date()}', label, 'US', 'release', stamp.date(), stamp.date(),
                          BEA_URL, '美国经济分析局 BEA', 'official/bea/release-calendar/v1', stamp=stamp))
    if not rows:
        raise ValueError('BEA important release schedule missing')
    return rows


def parse_bls(html):
    """Normal public monthly calendar only; a 403 is recorded, never bypassed."""
    rows = []
    soup = BeautifulSoup(html, 'html.parser')
    labels = {'Employment Situation': '美国非农就业与失业率', 'Consumer Price Index': '美国 CPI',
              'Producer Price Index': '美国 PPI', 'Employment Cost Index': '美国雇佣成本指数',
              'Job Openings and Labor Turnover Survey': '美国 JOLTS 职位空缺'}
    for tr in soup.select('table tr'):
        cells = [c.get_text(' ', strip=True) for c in tr.find_all(['td', 'th'], recursive=False)]
        if len(cells) != 3:
            continue
        label = next((k for k in labels if cells[2].startswith(k + ' for ')), None)
        if not label:
            continue
        stamp = datetime.strptime(cells[0] + ' ' + cells[1], '%A, %B %d, %Y %I:%M %p').replace(tzinfo=ZoneInfo('America/New_York'))
        url = BLS_URL.format(year=stamp.year, month=stamp.month)
        rows.append(event('bls-' + hashlib.sha256((label + str(stamp.date())).encode()).hexdigest()[:20],
                          labels[label] + '：' + cells[2], 'US', 'release', stamp.date(), stamp.date(), url,
                          '美国劳工统计局 BLS', 'official/bls/release-calendar/v1', stamp=stamp))
    if not rows:
        raise ValueError('BLS important release schedule missing')
    return rows


def parse_cn_meeting(html, url):
    if urlparse(url).hostname not in {'www.mofcom.gov.cn', 'www.gov.cn', 'www.news.cn', 'www.npc.gov.cn', 'www.cppcc.gov.cn'}:
        raise ValueError('meeting publisher not allowlisted')
    soup = BeautifulSoup(html, 'html.parser')
    text = re.sub(r'\s+', '', soup.get_text(' ', strip=True))
    # Only an explicit decision/announcement is accepted, not an analyst's estimate.
    year = re.search(r'(20\d{2})[-年]\d{1,2}[-月]\d{1,2}', text)
    pattern = r'(中国共产党第[一二三四五六七八九十]+届中央委员会第[一二三四五六七八九十]+次全体会议|中央经济工作会议|全国人民代表大会[^，。]{0,12}会议|中国人民政治协商会议[^，。]{0,15}会议)(?:将|定)?于(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日?(?:至(?:(\d{1,2})月)?(\d{1,2})日)?[^。]{0,20}(?:召开|举行)'
    rows = []
    for m in re.finditer(pattern, text):
        if not (m[2] or year):
            continue
        y = int(m[2] or year[1]); start = date(y, int(m[3]), int(m[4])); end = date(y, int(m[5] or m[3]), int(m[6] or m[4]))
        if end < start or (end - start).days > 30:
            raise ValueError('invalid official meeting range')
        identity = hashlib.sha256((m[1] + str(start)).encode()).hexdigest()[:20]
        rows.append(event('cn-meeting-' + identity, m[1], 'CN', 'meeting', start, end, url,
                          '中央会议公告（官方发布/新华社原文）', 'official/cn/meeting-announcements/v1'))
    return rows


def meeting_listing(source):
    soup = BeautifulSoup(source.get(CN_LIST), 'html.parser')
    # The public page itself specifies this GET request. It is an ordinary
    # publisher content fragment; no browser challenge or access control bypass.
    loader = soup.find('script', attrs={'url': '/api-gateway/jpaas-publish-server/front/page/build/unit'})
    if loader and loader.get('querydata'):
        params = ast.literal_eval(loader['querydata'])
        if not isinstance(params, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 200 for k, v in params.items()):
            raise ValueError('invalid official listing parameters')
        if set(params) != {'parseType', 'webId', 'tplSetId', 'pageType', 'tagId', 'editType', 'pageId'}:
            raise ValueError('unexpected official listing parameters')
        fragment = source.json(urljoin(CN_LIST, loader['url']), params)
        soup = BeautifulSoup(fragment['data']['html'], 'html.parser')
    anchors = [a for a in soup.select('a[href]') if '/xwfb/ldrhd/art/' in urljoin(CN_LIST, a['href'])]
    if not anchors:
        raise ValueError('official meeting announcement listing is empty/unverified')
    urls = []
    for a in anchors:
        title = a.get_text('', strip=True)
        url = urljoin(CN_LIST, a['href'])
        if urlparse(url).hostname == 'www.mofcom.gov.cn' and any(word in title for word in ('政治局召开会议', '经济工作会议', '全国人大', '全国政协')):
            urls.append(url)
    return urls


def collect(source, cfg, *, today=None):
    today = today or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    results = []
    def ingest(key, action):
        feed = 'important:' + key
        try:
            rows = action()
            if len({r['id'] for r in rows}) != len(rows):
                raise ValueError('duplicate important calendar event')
            count = store(feed, dict(source='important/' + key + '/v1', items=rows))
            results.append(dict(feature=feed, status='complete', rows=count))
        except Exception as exc:
            db.feed_attempt(feed, 'failed', type(exc).__name__)
            results.append(dict(feature=feed, status='failed', error=type(exc).__name__))
    nbs_url = cfg.get('nbs_schedule_url', NBS_URL)
    if urlparse(nbs_url).hostname != 'www.stats.gov.cn':
        raise ValueError('NBS schedule URL must be official')
    ingest('nbs', lambda: parse_nbs(source.get(nbs_url), today.year, nbs_url))
    ingest('bea', lambda: parse_bea(source.get(BEA_URL)))
    def bls():
        # Four monthly pages, including the year boundary. All must parse before replacement.
        months = [(today.replace(day=1) + timedelta(days=32 * i)).replace(day=1) for i in range(4)]
        return [r for day in months for r in parse_bls(source.get(BLS_URL.format(year=day.year, month=day.month)))]
    ingest('bls', bls)
    def fed():
        return [event(r['id'], r['title'], 'US', 'meeting', r['start_date'], r['end_date'], FED_URL,
                      '美联储 FOMC', 'official/federalreserve/fomc-calendar') for r in parse_fomc(source.get(FED_URL), today.year)]
    ingest('fed', fed)
    def cn():
        urls = list(dict.fromkeys([*CN_SEEDS, *cfg.get('important_meeting_urls', [])]))
        # Listing discovery is bounded; a list failure must not discard known verified articles.
        discovery_failed = False
        try:
            urls.extend(meeting_listing(source))
        except Exception:
            discovery_failed = True
        current = []
        for url in list(dict.fromkeys(urls))[:12]:
            current.extend(parse_cn_meeting(source.get(url), url))
        old = []
        try:
            old = load('important:cn-meetings')['items']
        except HTTPException:
            pass
        merged = {r['id']: r for r in old if r['end_date'] >= str(today - timedelta(days=370))}
        merged.update({r['id']: r for r in current})
        if not current or discovery_failed:
            # Retain verified dates; signal incomplete discovery separately, never claim full coverage.
            if current:
                store('important:cn-meetings', dict(source='important/cn-meetings/v1', items=list(merged.values())))
            raise ValueError('meeting listing discovery incomplete')
        return list(merged.values())
    ingest('cn-meetings', cn)
    return results


def report(start, end, country=None, category=None, limit=500):
    if start > end or (end - start).days > 180:
        raise HTTPException(422, 'important calendar range must be ordered and within 180 days')
    rows, states = [], []
    for key in SOURCES:
        try:
            payload = load('important:' + key)
            stale = payload['fetch_stale'] or payload['last_attempt_status'] != 'complete'
            states.append(dict(source=key, status='retained' if stale else 'current', collected_at=payload['collected_at']))
            for row in payload['items']:
                match_start = row.get('local_date') or row['start_date']
                match_end = row.get('local_date') or row['end_date']
                if match_end >= str(start) and match_start <= str(end):
                    rows.append(dict(row, fetch_stale=stale, collected_at=payload['collected_at']))
        except HTTPException:
            states.append(dict(source=key, status='unavailable', collected_at=None))
    # BLS may reject anonymous access. Keep the independent high-importance
    # aggregator as an explicit fallback, never relabel it an official schedule.
    if not any(r['source'] == 'official/bls/release-calendar/v1' for r in rows):
        try:
            payload = load('calendar:wscn')
            for row in payload['items']:
                if row['country_code'] == 'US' and row['importance'] >= 3 and re.search(r'非农|CPI|PPI|失业率|JOLTS', row['title'], re.I) and str(start) <= row['local_date'] <= str(end):
                    item = event('wscn-' + row['id'], row['title'], 'US', 'release', row['local_date'], row['local_date'],
                                 payload['source_page'], '华尔街见闻（经济日历）', row['source'],
                                 stamp=datetime.fromisoformat(row['occurs_at']), zone='UTC')
                    rows.append(dict(item, authority='aggregator', importance=row['importance'],
                                     fetch_stale=payload['fetch_stale'] or payload['last_attempt_status'] != 'complete', collected_at=payload['collected_at']))
            states.append(dict(source='wscn-bls-fallback', status='partial_window', collected_at=payload['collected_at'], end_date=payload['end_date']))
        except HTTPException:
            pass
    filtered = [r for r in rows if (not country or r['country_code'] == country) and (not category or r['category'] == category)]
    filtered.sort(key=lambda r: (r.get('local_date') or r['start_date'], r.get('occurs_at') or '', r['id']))
    return dict(items=filtered[:limit], total=len(filtered), truncated=len(filtered) > limit, requested_start=str(start), requested_end=str(end),
                sources=states, exhaustive=False, automatic_collection_enabled=False,
                pending_meetings=[dict(country_code='CN', title='中央经济工作会议 / 后续中央重要会议', status='awaiting_official_announcement', start_date=None)],
                note='仅已取得来源明确日程；不保证穷尽。日期型会议保留当地日期，不推算时刻；待公布日期不放入日历。')
