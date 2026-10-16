"""Bounded public Tencent observations, with explicit identity and price basis."""
import re
from datetime import date, datetime, time, timedelta

from . import normalize
from .calendars import CN, session_close
from .macro_assets import finite

KLINE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get'
MINUTE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/day/query'


def etf_rows(payload, item, target, days, adjustment=''):
    symbol=item['source_code']
    if item['kind']!='etf' or not re.fullmatch(r'(?:sh5|sz1)\d{5}',symbol):
        raise ValueError('exchange ETF identity required')
    if payload.get('code')!=0 or symbol not in payload.get('data',{}):
        raise ValueError('Tencent ETF response identity mismatch')
    body=payload['data'][symbol]
    quote=body.get('qt',{}).get(symbol)
    if not isinstance(quote,list) or len(quote)<3 or quote[2]!=item['symbol']:
        raise ValueError('Tencent ETF quote symbol mismatch')
    name='qfqday' if adjustment=='qfq' else 'day'
    # The publisher also uses "day" for a requested adjusted series with no
    # adjustment events; this is the same fallback used by the pinned SDK.
    raw=body.get(name,body.get('day') if adjustment=='qfq' else None)
    if not isinstance(raw,list) or not raw:
        raise normalize.PendingData('ETF daily observations unavailable')
    rows=[]
    for values in raw:
        if not isinstance(values,list) or len(values)<6:
            raise ValueError('Tencent ETF field layout changed')
        stamp=date.fromisoformat(values[0]).isoformat()
        if stamp not in days:
            continue
        rows.append(dict(日期=stamp,开盘=values[1],收盘=values[2],最高=values[3],最低=values[4],
            成交量=values[5],成交额=finite(values[8])*10000 if len(values)>8 and values[8] not in ('',None) else None,
            volume_unit='source_unit_unverified'))
    return normalize.bars(rows,str(target),'etf','CN')


def fetch_etf(source,item,target,days,adjustment=''):
    if adjustment not in ('','qfq'):
        raise ValueError('unsupported ETF adjustment')
    payload=source.json(KLINE_URL,dict(param=','.join([item['source_code'],'day',days[0],str(target),str(len(days)+10),adjustment])))
    return etf_rows(payload,item,target,set(days),adjustment)


def minute_rows(payload, symbol, cfg, target=None):
    if not re.fullmatch(r'(?:sh|sz|bj)\d{6}',symbol) or payload.get('code')!=0:
        raise ValueError('invalid minute response')
    body=payload.get('data',{}).get(symbol)
    if not isinstance(body,dict) or not isinstance(body.get('data'),list):
        raise ValueError('minute response identity mismatch')
    result={}
    now=datetime.now(CN)
    for session in body['data']:
        day=datetime.strptime(session['date'],'%Y%m%d').date()
        if target is not None and str(day)!=str(target):
            continue
        if day>now.date() or session_close('CN',day,cfg) is None:
            raise ValueError('minute date is not an observed market session')
        rows=[];previous_volume=0.;previous_amount=0.;seen=set()
        for line in session['data']:
            fields=line.split()
            if len(fields) not in (3,4) or not re.fullmatch(r'\d{4}',fields[0]):
                raise ValueError('minute field layout changed')
            clock=datetime.strptime(fields[0],'%H%M').time()
            if not (time(9,30)<=clock<=time(11,30) or time(13)<=clock<=time(15)):
                continue  # Preserve continuous-session observations only; no synthetic points.
            if fields[0] in seen:
                raise ValueError('duplicate minute')
            stamp=datetime.combine(day,clock,CN)
            if stamp>now+timedelta(minutes=1):
                raise ValueError('minute timestamp is in the future')
            close,volume=map(finite,fields[1:3])
            amount=finite(fields[3]) if len(fields)==4 else None
            if close<=0 or volume<previous_volume:
                raise ValueError('invalid minute price or cumulative counters')
            # The publisher occasionally revises cumulative turnover down by a
            # few yuan while volume is unchanged. Preserve its observation,
            # mark the revision and withhold an invalid negative interval flow.
            revised=amount is not None and amount<previous_amount
            rows.append(dict(timestamp=stamp.isoformat(),close=close,volume=volume-previous_volume,
                amount=amount-previous_amount if amount is not None and not revised else None,cumulative_volume=volume,cumulative_amount=amount,
                amount_counter_revised=revised,average=None))
            previous_volume,previous_amount=volume,amount if amount is not None else previous_amount;seen.add(fields[0])
        if rows:
            if rows!=sorted(rows,key=lambda row:row['timestamp']):
                raise ValueError('minute times are not ordered')
            result[str(day)]=rows
    if not result:
        raise normalize.PendingData('no minute observations')
    return result
