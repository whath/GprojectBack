"""Official FOMC agenda, observed revisions, and explicit date-only precision."""
import calendar
import hashlib
import json
import re
from datetime import date

from bs4 import BeautifulSoup
from fastapi import HTTPException

from . import db
from .supplements import save, saved

URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
SOURCE = 'official/federalreserve/fomc-calendar'


def parse_fomc(html, year):
    soup = BeautifulSoup(html, 'html.parser')
    months = {name: n for n, name in enumerate(calendar.month_name) if name}
    result = []
    for panel in soup.select('.panel'):
        heading = panel.select_one('.panel-heading')
        match = re.search(r'(\d{4}) FOMC Meetings', heading.get_text(' ', strip=True) if heading else '')
        if not match or int(match[1]) not in (year, year + 1):
            continue
        event_year = int(match[1])
        for meeting in panel.select('.fomc-meeting'):
            month_node = meeting.select_one('.fomc-meeting__month')
            date_node = meeting.select_one('.fomc-meeting__date')
            if month_node is None or date_node is None:
                raise ValueError('FOMC markup missing meeting dates')
            month_text = month_node.get_text(' ', strip=True)
            date_text = date_node.get_text(' ', strip=True)
            month_names = month_text.split('/')
            if any(m not in months for m in month_names):
                raise ValueError('unrecognized FOMC month')
            match_date = re.fullmatch(r'(\d{1,2})(?:-(\d{1,2}))?(\*)?', date_text)
            if not match_date:
                raise ValueError('unrecognized FOMC meeting date')
            start = date(event_year, months[month_names[0]], int(match_date[1]))
            end = date(event_year, months[month_names[-1]], int(match_date[2] or match_date[1]))
            if end < start:
                raise ValueError('FOMC meeting date order')
            result.append(dict(id='fed-fomc-' + end.isoformat(), start_date=start.isoformat(),
                               end_date=end.isoformat(), occurs_on=end.isoformat(), occurs_at=None,
                               time_precision='date', timezone='America/New_York', kind='scheduled',
                               title='FOMC 议息会议', markets=['US'], importance='high',
                               source=SOURCE, url=URL, published_at=None,
                               includes_projections=bool(match_date[3]),
                               source_date_text=month_text + ' ' + date_text,
                               presence='present_in_latest_source'))
    if len(result) < 8 or len({r['id'] for r in result}) != len(result):
        raise ValueError('FOMC calendar incomplete or duplicate')
    return sorted(result, key=lambda r: r['occurs_on'])


def store_fomc(rows):
    try:
        previous = {r['id']: r for r in saved('scheduled:fed-fomc', limit=200)['items']}
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        previous = {}
    now = db.now_iso()
    current = {}
    for raw in rows:
        row = dict(raw)
        old = previous.get(row['id'])
        ignored = {'observed_at', 'updated_at', 'first_observed_at', 'collected_at'}
        changed = old is None or {k: v for k, v in old.items() if k not in ignored} != row
        row.update(first_observed_at=old['first_observed_at'] if old else now,
                   updated_at=now if changed else old['updated_at'], observed_at=now)
        current[row['id']] = row
    # Source removal is evidence of disappearance, not proof of cancellation/postponement.
    for identifier, old in previous.items():
        if identifier not in current and old['start_date'][:4] in {r['start_date'][:4] for r in rows}:
            row = {k: v for k, v in old.items() if k != 'collected_at'}
            if row['presence'] != 'removed_from_latest_source':
                row.update(presence='removed_from_latest_source', updated_at=now)
            current[identifier] = dict(row, observed_at=now)
    return save('scheduled:fed-fomc', dict(source=SOURCE, url=URL, coverage='FOMC meetings only',
                 limitations=['date-only agenda; exact release time not supplied by this feed',
                              'publication time unavailable; observation time is separate',
                              'source removal does not certify cancellation or postponement',
                              'CPI/nonfarm/JP/KR/energy calendars not included']), list(current.values()), 'id', replace=True)


def collect(year, raw_dir=None):
    import requests
    from pathlib import Path
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(URL, timeout=20)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError('oversized official calendar')
    if raw_dir:
        folder = Path(raw_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'fomc.html').write_text(response.text, encoding='utf-8')
    count = store_fomc(parse_fomc(response.text, year))
    db.feed_attempt('scheduled:fed-fomc', 'complete')
    return dict(status='complete', source=SOURCE, rows=count, sha256=hashlib.sha256(response.content).hexdigest())


def report(start, end, limit=100, include_removed=False):
    if start > end:
        raise HTTPException(422, 'start must not exceed end')
    payload = saved('scheduled:fed-fomc', limit=200)
    items = [r for r in payload['items'] if r['end_date'] >= start.isoformat() and r['start_date'] <= end.isoformat()
             and (include_removed or r['presence'] == 'present_in_latest_source')]
    items.sort(key=lambda r: (r['occurs_on'], r['id']))
    return dict(payload, items=items[:limit], total=len(items), truncated=len(items) > limit)
