"""Dated A-share minute snapshots. API reads never contact the publisher."""
import re
from datetime import date, datetime, time

from fastapi import HTTPException

from . import db
from .calendars import latest_ready, session_close, validate_collection
from .normalize import PendingData
from .public_feeds import load, store
from .tencent_data import MINUTE_URL, minute_rows


def stock(identifier):
    if not re.fullmatch(r'CN\.stock\.\d{6}',identifier):
        raise HTTPException(422,'A-share stock identifier required')
    with db.connect() as con:
        item=con.execute('SELECT * FROM instruments WHERE id=?',(identifier,)).fetchone()
    if not item:raise HTTPException(404,'unknown stock')
    return dict(item)


def collect(source,identifier,day,cfg):
    validate_collection('CN',day,cfg)
    item=stock(identifier)
    payload=source.json(MINUTE_URL,dict(code=item['source_code']))
    observed=minute_rows(payload,item['source_code'],cfg,day)
    points=observed.get(str(day))
    if not points:raise PendingData('requested date absent from recent minute window')
    with db.connect() as con:
        daily=con.execute("SELECT close FROM bars WHERE instrument_id=? AND trade_date=? AND adjustment='none'",(identifier,str(day))).fetchone()
    if not daily:raise PendingData('daily close required to cross-check minute identity')
    if points[-1]['timestamp'][11:16]!='15:00' or abs(points[-1]['close']-daily['close'])>0.011:
        raise ValueError('minute close does not match verified daily close')
    result=store('intraday:'+identifier+':'+str(day),dict(instrument_id=identifier,trade_date=str(day),
        source='public/tencent-minute/v1',source_page='https://gu.qq.com/'+item['source_code'],
        period='1m',chart_type='line',adjustment='none',timezone='Asia/Shanghai',currency='CNY',
        volume_unit='source_unit_unverified',amount_unit='yuan',cross_checked_daily_close=daily['close'],
        coverage='publisher observations during continuous sessions; no interpolation; latest captured closed session',
        collection_on_read=False,items=points))
    return result


def report(identifier,day,cfg):
    stock(identifier)
    if day>latest_ready('CN',cfg) or session_close('CN',day,cfg) is None:
        raise HTTPException(422,'completed CN market session required')
    try:
        result=load('intraday:'+identifier+':'+str(day))
    except HTTPException as exc:
        if exc.status_code!=503:raise
        with db.connect() as con:
            halted=con.execute("SELECT 1 FROM datasets WHERE dataset='suspensions' AND market='CN' AND symbol=? AND trade_date=?",(identifier.rsplit('.',1)[-1],str(day))).fetchone()
            listed=con.execute('SELECT listed_date FROM instrument_lifecycle WHERE instrument_id=?',(identifier,)).fetchone()
        reason='suspended' if halted else ('not_listed_at_target' if listed and listed[0] and listed[0]>str(day) else None)
        if reason:
            return dict(instrument_id=identifier,trade_date=str(day),items=[],status='not_traded',reason=reason,period='1m',collection_on_read=False)
        raise
    try:
        if result['source']!='public/tencent-minute/v1' or result['instrument_id']!=identifier or result['trade_date']!=str(day):
            raise ValueError('minute snapshot identity mismatch')
        points=result['items']
        timestamps=[point['timestamp'] for point in points]
        if not points or len(timestamps)!=len(set(timestamps)) or timestamps!=sorted(timestamps):raise ValueError('invalid minute times')
        if any(not stamp.startswith(str(day)+'T') or not stamp.endswith('+08:00') for stamp in timestamps):raise ValueError('invalid minute date/timezone')
        clocks=[datetime.fromisoformat(stamp).time() for stamp in timestamps]
        if len(points)>242 or any(not (time(9,30)<=clock<=time(11,30) or time(13)<=clock<=time(15)) for clock in clocks):raise ValueError('invalid continuous session minute')
        if clocks[-1]!=time(15):raise ValueError('minute snapshot has no closing observation')
        from .macro_assets import finite
        if any(finite(point['close'])<=0 for point in points):raise ValueError('invalid minute price')
        with db.connect() as con:
            daily=con.execute("SELECT close FROM bars WHERE instrument_id=? AND trade_date=? AND adjustment='none'",(identifier,str(day))).fetchone()
        if not daily or abs(finite(points[-1]['close'])-daily[0])>.011:raise ValueError('minute/daily close mismatch')
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,'stored minute snapshot failed validation') from None
    return dict(result,status='current',point_count=len(points),collection_on_read=False)
