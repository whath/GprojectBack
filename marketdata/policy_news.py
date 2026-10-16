"""Dated official policy/news lists. No inferred timestamps or policy beneficiaries."""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from fastapi import HTTPException

from . import db
from .calendars import CN, latest_ready
from .history import window_start
from .public_feeds import load, store

SOURCES = {
    'gov': ('中国政府网', 'https://www.gov.cn/zhengce/', r'/zhengce/(?:content/)?\d{6}/content_\d+\.htm'),
    'pbc': ('中国人民银行', 'https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html', r'/goutongjiaoliu/113456/113469/\d+/index\.html'),
    'csrc': ('中国证监会', 'https://www.csrc.gov.cn/', r'/csrc/c\d+/c\d+/content\.shtml'),
    'ndrc': ('国家发展改革委', 'https://www.ndrc.gov.cn/xxgk/zcfb/tz/', r'/xxgk/(?:zcfb|jd)/.*?/t\d{8}_\d+\.html'),
    'mofcom': ('商务部', 'https://www.mofcom.gov.cn/zcfb/', r'/zcfb/(?:zc|jd)/art/\d{4}/art_[a-z0-9]+\.html'),
}
DATE = re.compile(r'(?<!\d)(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)')


def category(key, title, url):
    if '解读' in title or '答记者问' in title or '/jd/' in url:
        return 'interpretation'
    if key in ('gov','ndrc','mofcom'):
        return 'policy_document'
    return 'official_news'


def parse_listing(text, key, today):
    publisher, page, pattern = SOURCES[key]
    soup = BeautifulSoup(text, 'html.parser')
    if '\ufffd' in soup.get_text()[:200]:
        raise ValueError('policy text encoding is invalid')
    result = {}
    for a in soup.select('a[href]'):
        if key=='gov' and a.parent.name=='p':
            continue  # Teaser paragraphs link to the same URL as the actual headline.
        title = ' '.join((a.get('title') or a.get_text(' ', strip=True)).split())
        parts = urlsplit(urljoin(page, a.get('href','').strip()))
        if parts.hostname != urlsplit(page).hostname or parts.username or parts.password or parts.port not in (None,443):
            continue
        if not re.fullmatch(pattern, parts.path) or len(title)<5:
            continue
        # Search only a small article/list container; page headers cannot supply a date.
        stamp = None
        for parent in list(a.parents)[:4]:
            if parent.name not in ('li','td','div'):
                continue
            content = parent.get_text(' ', strip=True)
            if len(content)>1200:
                break
            dates = {date(*map(int,m.groups())).isoformat() for m in DATE.finditer(content)}
            if len(dates)==1:
                stamp = dates.pop()
                break
        if not stamp:
            continue  # A month/day label is not a verified publication year.
        if stamp>today.isoformat():
            raise ValueError('future policy publication date')
        link = urlunsplit(('https',parts.netloc,parts.path,'',''))
        identity = re.search(r'/c(\d+)/content\.shtml',parts.path) if key=='csrc' else None
        identifier = key+':'+(identity[1] if identity else hashlib.sha256(link.encode()).hexdigest())
        row = dict(id=identifier,title=title,publisher=publisher,original_url=link,published_date=stamp,
                   published_at=None,time_precision='date',effective_date=None,document_number=None,
                   category=category(key,title,link),source='official/'+key+'/policy-news/v1',summary=None)
        if key=='gov' and a.find_parent('h5') is not None:
            row['category']='interpretation'
        old = result.get(identifier)
        if old and any(old[k]!=row[k] for k in ('title','published_date')):
            raise ValueError('conflicting official policy identity')
        result.setdefault(identifier,row)
    if not result:
        raise ValueError('no qualified full-date official entries')
    return sorted(result.values(),key=lambda r:(r['published_date'],r['id']),reverse=True)


def collect(source, cfg, *, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(CN).date()
    start = window_start('CN',latest_ready('CN',cfg,now),cfg).isoformat()
    results = []
    for key, (_,page,_) in SOURCES.items():
        try:
            rows = parse_listing(source.get(page),key,today)
            with db.connect() as con:
                old = con.execute('SELECT payload FROM feed_snapshots WHERE feed_key=?',('policy:'+key,)).fetchone()
            previous = json.loads(old[0])['items'] if old else []
            merged = {r['id']:r for r in previous if r['published_date']>=start}
            merged.update({r['id']:r for r in rows})
            rows = sorted(merged.values(),key=lambda r:(r['published_date'],r['id']),reverse=True)[:300]
            count = store('policy:'+key,dict(source='official/'+key+'/policy-news/v1',source_page=page,
                          coverage='dated official listing observations; not an exhaustive archive',items=rows))
            results.append(dict(feature='policy:'+key,status='complete',rows=count))
        except Exception as exc:
            db.feed_attempt('policy:'+key,'failed',type(exc).__name__)
            results.append(dict(feature='policy:'+key,status='failed',error=type(exc).__name__))
    return results


def report(start, end, publisher=None, category_filter=None):
    if start>end or (end-start).days>90:
        raise HTTPException(422,'policy dates must be ordered and within 90 days')
    rows, states = [], []
    for key in SOURCES:
        if publisher and key!=publisher:
            continue
        try:
            payload = load('policy:'+key)
            if payload['source']!='official/'+key+'/policy-news/v1':
                raise ValueError('invalid stored policy source')
            for row in payload['items']:
                date.fromisoformat(row['published_date'])
                if row['source']!=payload['source'] or row['published_at'] is not None or row['time_precision']!='date':
                    raise ValueError('invalid stored policy provenance')
                if start.isoformat()<=row['published_date']<=end.isoformat() and (not category_filter or row['category']==category_filter):
                    rows.append(dict(row,collected_at=payload['collected_at']))
            states.append(dict(source=key,fetch_stale=payload['fetch_stale'],last_attempt_status=payload['last_attempt_status'],
                               collected_at=payload['collected_at']))
        except HTTPException as exc:
            if exc.status_code!=503:
                raise
            states.append(dict(source=key,status='not_collected'))
        except (KeyError,ValueError,TypeError):
            raise HTTPException(503,'stored policy data failed validation') from None
    rows.sort(key=lambda r:(r['published_date'],r['id']),reverse=True)
    return dict(items=rows,total=len(rows),sources=states,start_date=str(start),end_date=str(end),
                coverage='official listing observations with original links; not a complete policy archive')
