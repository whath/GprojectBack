"""Issuer allowlist for national policy, US official releases and public research."""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from fastapi import HTTPException

from . import db
from .calendars import CN, latest_ready
from .history import window_start
from .public_feeds import load, store

SOURCES={
    'fed':dict(publisher='美联储',region='US',level='central_bank',page='https://www.federalreserve.gov/feeds/press_all.xml',hosts=['www.federalreserve.gov'],format='rss'),
    'treasury':dict(publisher='美国财政部',region='US',level='government',page='https://home.treasury.gov/news/press-releases',hosts=['home.treasury.gov'],format='treasury'),
    'nvidia':dict(publisher='英伟达 NVIDIA',region='US',level='company',page='https://nvidianews.nvidia.com/releases.xml',hosts=['nvidianews.nvidia.com','blogs.nvidia.com'],format='rss'),
    'gs':dict(publisher='高盛 Goldman Sachs',region='US',level='research',page='https://www.goldmansachs.com/insights',hosts=['www.goldmansachs.com'],format='research'),
    'ms':dict(publisher='摩根士丹利 Morgan Stanley',region='US',level='research',page='https://www.morganstanley.com/insights',hosts=['www.morganstanley.com'],format='research'),
}
MONTH_DATE=re.compile(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+20\d{2}\b')
FINANCE=re.compile(r'AI|artificial intelligence|market|econom|treasur|bond|inflation|rate|invest|growth|data center|semiconductor|chip|capital|trade|earning|revenue|technology|bank|equity|stock|currency|monetary|fiscal|energy',re.I)


def checked_url(key,url):
    parts=urlsplit(urljoin(SOURCES[key]['page'],url))
    if parts.scheme!='https' or parts.hostname not in SOURCES[key]['hosts'] or parts.username or parts.password or parts.port not in (None,443):
        raise ValueError('news URL is outside the issuer allowlist')
    return parts._replace(fragment='').geturl()


def entry(key,title,url,published_date,published_at=None):
    title=' '.join(title.split())
    if len(title)<5 or len(title)>600:raise ValueError('invalid news title')
    day=date.fromisoformat(published_date)
    if day>datetime.now(CN).date():raise ValueError('future news date')
    if published_at:
        stamp=datetime.fromisoformat(published_at.replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp>datetime.now(timezone.utc):raise ValueError('unverified news timestamp')
        published_at=stamp.astimezone(timezone.utc).isoformat()
    url=checked_url(key,url)
    spec=SOURCES[key]
    return dict(id=key+':'+hashlib.sha256(url.encode()).hexdigest(),title=title,original_url=url,
        publisher=spec['publisher'],issuer=spec['publisher'],region=spec['region'],authority_level=spec['level'],
        category=spec['level'],published_date=str(day),published_at=published_at,
        time_precision='instant' if published_at else 'date',source='official/'+key+'/priority-news/v1',summary=None)


def rss_rows(text,key):
    document=ElementTree.fromstring(text)
    rows=[]
    for node in document.findall('./channel/item'):
        title=node.findtext('title','');link=node.findtext('link','');stamp=parsedate_to_datetime(node.findtext('pubDate',''))
        if stamp.tzinfo is None:raise ValueError('RSS date has no timezone')
        rows.append(entry(key,title,link,stamp.date().isoformat(),stamp.isoformat()))
    if not rows:raise ValueError('no dated issuer RSS entries')
    return rows


def treasury_rows(text):
    soup=BeautifulSoup(text,'html.parser');rows=[]
    for clock in soup.select('time[datetime]'):
        container=clock.find_parent(class_='views-row')
        if container is None:
            container=next((parent for parent in list(clock.parents)[:5] if len(parent.select('time[datetime]'))==1 and parent.select_one('a[href*="/news/press-releases/"]')),None)
        if container is None:continue
        link=container.select_one('a[href*="/news/press-releases/"]')
        if not link:continue
        stamp=datetime.fromisoformat(clock['datetime'].replace('Z','+00:00'))
        rows.append(entry('treasury',link.get_text(' ',strip=True),link['href'],stamp.date().isoformat(),stamp.isoformat()))
    if not rows:raise ValueError('no dated Treasury releases')
    return rows


def article_date(text):
    soup=BeautifulSoup(text,'html.parser')
    for node in soup.select('meta[property="article:published_time"],meta[name="content_publishedAt"],meta[name="date"],meta[name="pubdate"],meta[name="publishdate"],meta[name="dc.date"],meta[name="dcterms.date"]'):
        value=node.get('content','')
        if re.match(r'^20\d\d-\d\d-\d\d',value):return date.fromisoformat(value[:10]).isoformat()
    def walk(value):
        if isinstance(value,dict):
            if isinstance(value.get('datePublished'),str) and re.match(r'^20\d\d-\d\d-\d\d',value['datePublished']):return value['datePublished'][:10]
            for child in value.values():
                found=walk(child)
                if found:return found
        if isinstance(value,list):
            for child in value:
                found=walk(child)
                if found:return found
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            found=walk(json.loads(script.get_text()))
            if found:return date.fromisoformat(found).isoformat()
        except (ValueError,TypeError):continue
    return None


def research_rows(text,key,source,max_articles=16):
    soup=BeautifulSoup(text,'html.parser');rows={};attempted=0
    for link in soup.select('a[href]'):
        path=urlsplit(urljoin(SOURCES[key]['page'],link['href'])).path
        if not (path.startswith('/insights/articles/') or (key=='ms' and path.startswith('/ideas/') and path.count('/')==2)):
            continue
        title=link.get_text(' ',strip=True)
        if len(title)<10 or not FINANCE.search(title):continue
        url=checked_url(key,link['href'])
        if url in rows:continue
        match=MONTH_DATE.search(title)
        stamp=datetime.strptime(match[0],'%b %d, %Y').date().isoformat() if match else None
        if not stamp:
            if attempted>=max_articles:continue
            attempted+=1
            try:stamp=article_date(source.get(url))
            except Exception:continue  # No access/login circumvention; retain other verified sources.
        if not stamp:continue
        title=MONTH_DATE.sub('',title).strip(' |')
        rows[url]=entry(key,title,url,stamp)
    if not rows:raise ValueError('no independently dated public research entries')
    return list(rows.values())


def qualified_cn(row,key):
    from .policy_news import SOURCES as CN_SOURCES
    if key not in CN_SOURCES:return False
    if row.get('publisher')!=CN_SOURCES[key][0]:return False
    try:
        if date.fromisoformat(row['published_date'])>datetime.now(CN).date():return False
    except (KeyError,ValueError,TypeError):return False
    parts=urlsplit(row.get('original_url',''))
    if parts.scheme!='https' or parts.hostname!=urlsplit(CN_SOURCES[key][1]).hostname or parts.username or parts.password or parts.port not in (None,443):return False
    if not re.fullmatch(CN_SOURCES[key][2],parts.path):return False
    if key=='gov':
        return bool(re.match(r'国务院|中共中央|中央军委|全国人大|中华人民共和国(?:主席|法律)',row['title'])) and row.get('category')=='policy_document'
    # National ministry pages only; do not elevate local bureaus by domain alone.
    if re.search(r'证监局|监管局|分行|分局|驻[\u4e00-\u9fff]{2,8}(?:办|特派)',row['title']):return False
    return bool(re.search(r'通知|公告|意见|办法|规定|指引|政策|决定|批复|通报|征求意见|答记者问|统计数据|报告|货币政策委员会|调整|实施|印发',row['title']))


def collect(source,cfg):
    cutoff=window_start('CN',latest_ready('CN',cfg),cfg).isoformat();results=[]
    from .policy_news import SOURCES as CN_SOURCES
    for key in CN_SOURCES:
        try:
            original=load('policy:'+key);rows=[]
            for item in original['items']:
                if item['published_date']<cutoff or not qualified_cn(item,key):continue
                issuer='国务院／中央机关' if key=='gov' else item['publisher']
                rows.append(dict(item,issuer=issuer,region='CN',authority_level='central' if key=='gov' else 'ministry',
                    source='official/'+key+'/priority-news/v1'))
            if not rows:raise ValueError('no national-level policy releases in current window')
            store('priority-news:'+key,dict(source='official/'+key+'/priority-news/v1',items=rows,
                policy='central or national ministry publication pages; local bureaus excluded',source_page=CN_SOURCES[key][1]))
            results.append(dict(key=key,status='complete',rows=len(rows)))
        except Exception as exc:
            db.feed_attempt('priority-news:'+key,'failed',type(exc).__name__);results.append(dict(key=key,status='failed',error=type(exc).__name__))
    for key,spec in SOURCES.items():
        try:
            text=source.get(spec['page'])
            rows=rss_rows(text,key) if spec['format']=='rss' else (treasury_rows(text) if key=='treasury' else research_rows(text,key,source))
            rows={row['id']:row for row in rows if row['published_date']>=cutoff}
            if not rows:raise ValueError('no recent dated releases')
            store('priority-news:'+key,dict(source='official/'+key+'/priority-news/v1',source_page=spec['page'],
                items=sorted(rows.values(),key=lambda row:(row['published_date'],row['id']),reverse=True),policy='exact issuer host allowlist; no reposts or undated content'))
            results.append(dict(key=key,status='complete',rows=len(rows)))
        except Exception as exc:
            db.feed_attempt('priority-news:'+key,'failed',type(exc).__name__);results.append(dict(key=key,status='failed',error=type(exc).__name__))
    return results


def report(start,end,region=None,level=None):
    if start>end or (end-start).days>90:raise HTTPException(422,'news window must be ordered and within 90 days')
    from .policy_news import SOURCES as CN_SOURCES
    rows=[];states=[]
    for key in [*CN_SOURCES,*SOURCES]:
        try:
            feed=load('priority-news:'+key)
            if feed['source']!='official/'+key+'/priority-news/v1':raise ValueError('unqualified news source')
            for row in feed['items']:
                if key in SOURCES:
                    checked_url(key,row['original_url'])
                    if row['authority_level']!=SOURCES[key]['level'] or row['publisher']!=SOURCES[key]['publisher']:raise ValueError('issuer mismatch')
                elif not qualified_cn(row,key):raise ValueError('unqualified national policy')
                if str(start)<=row['published_date']<=str(end) and (not region or row['region']==region) and (not level or row['authority_level']==level):
                    rows.append(dict(row,collected_at=feed['collected_at']))
            states.append(dict(source=key,status=feed['last_attempt_status'],collected_at=feed['collected_at'],fetch_stale=feed['fetch_stale']))
        except HTTPException as exc:
            if exc.status_code!=503:raise
            states.append(dict(source=key,status='not_collected'))
        except (KeyError,ValueError,TypeError):raise HTTPException(503,'stored priority news failed issuer validation') from None
    rows.sort(key=lambda row:(row['published_date'],row.get('published_at') or '',row['id']),reverse=True)
    return dict(items=rows,total=len(rows),sources=states,start_date=str(start),end_date=str(end),
        issuer_policy='CN central/national ministries; US government/Fed; allowlisted major research firms and companies',
        coverage='observed public original releases; no paywalled or exhaustive archive claim',collection_on_read=False)
