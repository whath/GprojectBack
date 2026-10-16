"""WSCN economic agenda; scheduled clocks are never called article publication times."""
import math
import re
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException

from . import db
from .calendars import CN, latest_ready
from .history import window_start
from .public_feeds import load, store

SOURCE = 'wallstreetcn/macro-calendar/v1'
PAGE = 'https://wallstreetcn.com/calendar'
URL = 'https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas'


def amount(raw):
    if raw is None or str(raw).strip() in ('', '-', '--', 'null'):
        return None
    if isinstance(raw, bool):
        raise ValueError('boolean economic value')
    text = str(raw).strip().replace(',', '')
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', text):
        return None  # Retain formatted values in *_raw and the source unit.
    result = float(text)
    if not math.isfinite(result):
        raise ValueError('nonfinite economic value')
    return result


def normalize(payload, start, end):
    if payload.get('code') != 20000 or not isinstance(payload.get('data',{}).get('items'), list):
        raise ValueError('economic source envelope mismatch')
    begin = datetime.combine(start, time(), CN).astimezone(timezone.utc)
    stop = datetime.combine(end+timedelta(days=1), time(), CN).astimezone(timezone.utc)
    items = {}
    for raw in payload['data']['items']:
        identifier = str(raw['id'])
        if not identifier.isdigit() or not str(raw.get('title','')).strip():
            raise ValueError('economic event identity missing')
        clock = raw['public_date']
        if isinstance(clock, bool) or not isinstance(clock, (int,float)) or not math.isfinite(clock):
            raise ValueError('invalid economic epoch')
        stamp = datetime.fromtimestamp(clock, timezone.utc)
        if not begin <= stamp < stop:
            raise ValueError('economic event outside requested window')
        importance = raw.get('importance')
        if isinstance(importance, bool) or importance not in (0,1,2,3,4):
            raise ValueError('invalid economic importance')
        item = dict(id='wscn:'+identifier, title=str(raw['title']).strip(), occurs_at=stamp.isoformat(),
                    local_date=stamp.astimezone(CN).date().isoformat(), timezone='UTC',
                    region=str(raw.get('country') or ''), country_code=str(raw.get('country_id') or ''),
                    importance=importance, unit=str(raw.get('unit') or '') or None,
                    period=str(raw.get('period') or '') or None,
                    category=str(raw.get('calendar_type') or ''), source=SOURCE,
                    source_page=PAGE, original_url=raw.get('uri') or None,
                    published_at=None, time_precision='second',
                    revision_status='source_latest_observation; absence is not cancellation')
        if item['original_url'] and not item['original_url'].startswith('https://'):
            item['original_url'] = None
        for name, key in [('actual','actual'),('expected','forecast'),('previous','previous'),('revised','revised')]:
            value = raw.get(key)
            item[name] = amount(value)
            item[name+'_raw'] = None if value is None or str(value).strip()=='' else str(value)
        if identifier in items and items[identifier] != item:
            raise ValueError('conflicting economic event identity')
        items[identifier] = item
    if not items:
        raise ValueError('empty economic window is unverified')
    return sorted(items.values(), key=lambda r:(r['occurs_at'],r['id']))


def collect(source, cfg, *, now=None):
    now = now or datetime.now(timezone.utc)
    start = window_start('CN', latest_ready('CN',cfg,now), cfg)
    end = now.astimezone(CN).date()+timedelta(days=7)
    try:
        # The public service rejects a long single range. Use bounded weekly
        # windows; UTC epochs avoid the SDK's dependence on the host timezone.
        items=[]
        cursor=start
        while cursor<=end:
            last=min(cursor+timedelta(days=6),end)
            params=dict(start=int(datetime.combine(cursor,time(),CN).timestamp()),
                        end=int(datetime.combine(last+timedelta(days=1),time(),CN).timestamp())-1)
            items.extend(normalize(source.json(URL,params),cursor,last))
            cursor=last+timedelta(days=1)
        if len({r['id'] for r in items})!=len(items):
            raise ValueError('duplicate calendar identity across weekly windows')
        with db.connect() as con:
            old = con.execute("SELECT payload FROM feed_snapshots WHERE feed_key='calendar:wscn'").fetchone()
        import json
        previous = {r['id']:r for r in json.loads(old[0]).get('items',[])} if old else {}
        stamp = db.now_iso()
        for item in items:
            prior = previous.get(item['id'])
            item['updated_at'] = prior['updated_at'] if prior and all(prior.get(k)==v for k,v in item.items()) else stamp
        count = store('calendar:wscn', dict(source=SOURCE, source_page=PAGE, start_date=str(start), end_date=str(end),
                      items=items, coverage='publisher-returned economic agenda within requested dates; not a certified global exhaustive calendar'))
        return dict(feature='economic_calendar', status='complete', rows=count)
    except Exception as exc:
        db.feed_attempt('calendar:wscn','failed',type(exc).__name__)
        return dict(feature='economic_calendar',status='failed',error=type(exc).__name__,message=str(exc)[:180])


def report(start, end, region=None):
    if start>end or (end-start).days>90:
        raise HTTPException(422,'calendar range must be ordered and within 90 days')
    payload = load('calendar:wscn')
    items = []
    try:
        if payload['source'] != SOURCE:
            raise ValueError('calendar source mismatch')
        for row in payload['items']:
            stamp = datetime.fromisoformat(row['occurs_at'])
            if stamp.tzinfo is None or row['source'] != SOURCE or row['published_at'] is not None:
                raise ValueError('invalid stored calendar provenance')
            if start.isoformat() <= row['local_date'] <= end.isoformat() and (not region or row['country_code']==region):
                items.append(row)
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503,'stored calendar failed validation') from None
    return dict(payload, items=items, total=len(items), requested_start=str(start), requested_end=str(end),
                window_covered=payload['start_date']<=str(start) and payload['end_date']>=str(end))
