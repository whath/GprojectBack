"""Eastmoney board taxonomy and dated main-order flow, kept separate from THS."""
import re
from datetime import date, datetime, timezone

from fastapi import HTTPException

from . import db
from .calendars import CN, latest_ready, validate_collection
from .history import sessions
from .macro_assets import finite
from .normalize import PendingData
from .public_feeds import load, store

SOURCE = 'eastmoney/board-main-order-flow/v1'


def current_members(source,code):
    """Code-sorted pages cannot overlap when intraday percentage rankings move."""
    from .normalize import members
    rows,total={},None
    for page in range(1,101):
        payload=source.json('https://push2.eastmoney.com/api/qt/clist/get',dict(
            pn=page,pz=100,po=0,np=1,fields='f12,f13,f14',fid='f12',fs=f'b:{code} f:!50'))
        body=payload.get('data')
        if payload.get('rc')!=0 or not isinstance(body,dict):
            raise ValueError('membership envelope mismatch')
        count=body.get('total')
        if isinstance(count,bool) or not isinstance(count,int) or not 0<count<10000 or (total is not None and total!=count):
            raise ValueError('membership total changed')
        total=count
        page_rows=body.get('diff')
        if isinstance(page_rows,dict):page_rows=list(page_rows.values())
        if not isinstance(page_rows,list) or not page_rows:
            raise ValueError('membership pagination ended early')
        for row in page_rows:
            symbol=str(row['f12'])
            if not re.fullmatch(r'\d{6}',symbol) or row['f13'] not in (0,1) or symbol in rows:
                raise ValueError('membership symbol/market mismatch or duplicate')
            rows[symbol]={'代码':symbol,'名称':str(row['f14']).strip()}
        if len(rows)==total:return [dict(r,source='public/eastmoney/membership/v1') for r in members([rows[k] for k in sorted(rows)])]
        if len(rows)>total:raise ValueError('membership exceeded total')
    raise ValueError('membership pagination budget exceeded')


def catalog(source, kind):
    result, total = {}, None
    for page in range(1,51):
        data = source.json('https://push2.eastmoney.com/api/qt/clist/get', dict(
            pn=page,pz=100,po=0,np=1,fields='f12,f13,f14',fid='f12',fs='m:90+t:'+('2' if kind=='industry' else '3')))
        if data.get('rc')!=0 or not isinstance(data.get('data'),dict):
            raise ValueError('board catalog envelope mismatch')
        body=data['data']
        count=body.get('total')
        if isinstance(count,bool) or not isinstance(count,int) or not 0<count<5000 or (total is not None and total!=count):
            raise ValueError('board catalog total changed')
        total=count
        rows=body.get('diff')
        if isinstance(rows,dict): rows=list(rows.values())
        if not isinstance(rows,list) or not rows:
            raise ValueError('board catalog ended before total')
        for row in rows:
            code,name=row['f12'],str(row['f14']).strip()
            if not re.fullmatch(r'BK\d{4}',code) or not name or row['f13']!=90 or code in result:
                raise ValueError('board catalog identity mismatch or duplicate')
            result[code]=dict(id=f'CN.{kind}.{code}',market='CN',kind=kind,symbol=code,
                             name=name,source_code=code,currency='CNY',group_name='eastmoney')
        if len(result)==total:
            return list(result.values())
        if len(result)>total:
            raise ValueError('board catalog exceeded total')
    raise ValueError('board catalog pagination budget exceeded')


def flow_rows(payload, code, target, cfg):
    data=payload.get('data')
    if payload.get('rc')!=0 or not isinstance(data,dict) or data.get('code')!=code or data.get('market')!=90:
        raise ValueError('board funds identity mismatch')
    expected=set(sessions('CN',target,30,cfg))
    rows={}
    for line in data.get('klines') or []:
        values=line.split(',')
        if len(values)!=15:
            raise ValueError('board funds field layout changed')
        day=date.fromisoformat(values[0]).isoformat()
        if day not in expected:
            continue
        net,large,super_large=(finite(values[i]) for i in (1,4,5))
        if abs(net-large-super_large)>max(1,abs(net)*1e-7):
            raise ValueError('board main-order definition mismatch')
        if day in rows:
            raise ValueError('duplicate board funds date')
        rows[day]=dict(trade_date=day,net=net,inflow=None,outflow=None,large_net=large,super_large_net=super_large)
    if not rows:
        raise PendingData('no qualified board funds observations')
    return [rows[d] for d in sorted(rows)]


def collect_flow(source, item, target, cfg):
    payload=source.json('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get',dict(
        lmt=40,klt=101,fields1='f1,f2,f3,f7',fields2=','.join('f'+str(n) for n in range(51,66)),
        secid='90.'+item['symbol']))
    rows=flow_rows(payload,item['symbol'],target,cfg)
    count=store('board-flows:'+item['id'],dict(board_id=item['id'],name=item['name'],classification_source='eastmoney',
                source=SOURCE,source_page='https://data.eastmoney.com/bkzj/'+item['symbol']+'.html',
                currency='CNY',unit='yuan',scope='main',definition='provider main = large + super-large net orders; not total cash entering the market',
                items=rows,window=30,target_date=str(target)))
    if rows[-1]['trade_date']!=str(target):
        raise PendingData('target board funds date absent; retained dated history')
    return count


def report(identifier, limit=30, end=None):
    from .settings import config
    if not re.fullmatch(r'CN\.(industry|concept)\.BK\d{4}',identifier):
        raise HTTPException(404,'unsupported board taxonomy')
    data=load('board-flows:'+identifier)
    if data.get('board_id')!=identifier or data.get('classification_source')!='eastmoney' or data.get('source')!=SOURCE or data.get('unit')!='yuan':
        raise HTTPException(503,'stored board funds provenance invalid')
    try:
        seen=set()
        for row in data['items']:
            day=date.fromisoformat(row['trade_date'])
            if day in seen or day>latest_ready('CN',config()):raise ValueError('duplicate or future funds date')
            seen.add(day)
            net,large,super_large=(finite(row[k]) for k in ('net','large_net','super_large_net'))
            if abs(net-large-super_large)>max(1,abs(net)*1e-7):raise ValueError('funds definition mismatch')
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,'stored board funds failed validation') from None
    cutoff=min(end or latest_ready('CN',config()),latest_ready('CN',config()))
    days=sessions('CN',cutoff,min(limit,30),config())
    rows=[r for r in data['items'] if r['trade_date'] in days]
    return dict(data,items=rows,trade_dates=days,latest_date=rows[-1]['trade_date'] if rows else None,
                updated_at=data['collected_at'],stale=not rows or rows[-1]['trade_date']!=str(cutoff))


def ranking(target, kind='industry', period=1):
    from .settings import config
    from .calendars import session_close
    if target>latest_ready('CN',config()) or session_close('CN',target,config()) is None:
        raise HTTPException(422,'completed CN market session required')
    days=sessions('CN',target,period,config())
    with db.connect() as con:
        expected=[dict(r) for r in con.execute("SELECT i.id,i.name,i.symbol FROM instruments i LEFT JOIN instrument_lifecycle l ON l.instrument_id=i.id WHERE i.market='CN' AND i.kind=? AND i.symbol LIKE 'BK%' AND COALESCE(l.status,'listed')='listed' ORDER BY i.id",(kind,))]
    rows=[]
    for item in expected:
        try:
            data=report(item['id'],period,target)
        except HTTPException as exc:
            if exc.status_code!=503: raise
            continue
        if {r['trade_date'] for r in data['items']}!=set(days): continue
        rows.append(dict(item,net=sum(finite(r['net']) for r in data['items']),trade_date=str(target),source=SOURCE,unit='yuan',currency='CNY'))
    rows.sort(key=lambda r:(-r['net'],r['id']))
    for i,row in enumerate(rows,1):row['rank']=i
    return dict(items=rows,total=len(rows),trade_date=str(target),period_sessions=period,classification_source='eastmoney',
                expected_count=len(expected),eligible_count=len(rows),excluded_count=len(expected)-len(rows),
                complete=bool(expected) and len(rows)==len(expected),scope='collected_qualified_boards',source=SOURCE)


def collect(source,cfg,target,max_items=10,*,all_boards=False):
    from .pipeline import Collector
    from .normalize import members
    validate_collection('CN',target,cfg)
    items=[]
    observed=datetime.now(CN).date().isoformat()
    for kind in ('industry','concept'):
        rows=catalog(source,kind)
        db.publish_catalog(rows,'CN',kind,observed,'public/eastmoney/catalog/v1','eastmoney',full=True)
        wanted=cfg['cn_boards'][kind]
        items += [r for r in rows if all_boards or r['symbol'] in wanted or r['name'] in wanted]
    # Resume uncollected boards before retrying a previously completed source.
    with db.connect() as con:
        collected={r[0] for r in con.execute("SELECT feed_key FROM feed_snapshots WHERE feed_key LIKE 'board-flows:%'")}
    items.sort(key=lambda r:('board-flows:'+r['id'] in collected,r['kind']!='industry',r['id']))
    collector=Collector('CN',target,settings=dict(cfg,request_attempts=1,request_timeout_seconds=35),refresh=True)
    results=[]
    for item in items[:max_items]:
        for feature,action in [
            ('bars',lambda i=item:collector.history(i,'stock_board_'+i['kind']+'_hist_em')),
            ('flows',lambda i=item:collect_flow(source,i,target,cfg)),
            ('members',lambda i=item:current_members(source,i['symbol']))
        ]:
            key='board:'+feature+':'+item['id']
            try:
                data=action()
                if feature=='members':
                    db.put_dataset('members:'+item['id'],'CN',observed,data)
                db.feed_attempt(key,'complete')
                results.append(dict(feature=key,status='complete',rows=len(data) if isinstance(data,list) else data))
            except Exception as exc:
                db.feed_attempt(key,'failed',type(exc).__name__)
                results.append(dict(feature=key,status='pending' if isinstance(exc,PendingData) else 'failed',error=type(exc).__name__,message=str(exc)[:180]))
    return results
