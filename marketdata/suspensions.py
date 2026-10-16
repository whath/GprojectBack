"""Verify full-session suspension; intraday pauses and resumed stocks do not qualify."""
import json
import re
from datetime import datetime,time
from . import db


def full_session(rows,day):
    start=datetime.fromisoformat(day+"T09:30:00")
    end=datetime.fromisoformat(day+"T15:00:00")
    result=[]
    for row in rows:
        try:
            begins=datetime.fromisoformat(row["SUSPEND_START_TIME"])
            finishes=datetime.fromisoformat(row["SUSPEND_END_TIME"]) if row.get("SUSPEND_END_TIME") else None
            resume=datetime.fromisoformat(row["PREDICT_RESUME_DATE"]) if row.get("PREDICT_RESUME_DATE") else None
            if begins>start or (finishes and finishes<end) or (resume and resume<=start):continue
            symbol=str(row["SECURITY_CODE"]).zfill(6)
            if not re.fullmatch(r'\d{6}',symbol):raise ValueError('invalid suspension symbol')
            result.append({"key":symbol,"symbol":symbol,"name":row["SECURITY_NAME_ABBR"],
                           "trade_date":day,"status":"reported_full_session_suspension",
                           "source":"eastmoney/suspension_report","reason":row["SUSPEND_REASON"],
                           "start":row["SUSPEND_START_TIME"],"end":row.get("SUSPEND_END_TIME")})
        except (KeyError,TypeError,ValueError):
            raise ValueError("invalid suspension report") from None
    return result


def precise(source, day):
    """Keep publisher timestamps; SDK date-only columns lose boundary evidence."""
    from datetime import date
    date.fromisoformat(day)
    rows=[];total=None;pages=None
    for page in range(1,21):
        payload=source.json('https://datacenter-web.eastmoney.com/api/data/v1/get',dict(
            sortColumns='SUSPEND_START_DATE',sortTypes='-1',pageSize=500,pageNumber=page,
            reportName='RPT_CUSTOM_SUSPEND_DATA_INTERFACE',columns='ALL',source='WEB',client='WEB',
            filter=f'(MARKET="全部")(DATETIME=\'{day}\')'))
        body=payload.get('result')
        if payload.get('success') is not True or not isinstance(body,dict):raise ValueError('suspension envelope mismatch')
        count,page_count=body.get('count'),body.get('pages')
        if isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=10000:raise ValueError('invalid suspension count')
        if isinstance(page_count,bool) or not isinstance(page_count,int) or not 0<=page_count<=20:raise ValueError('invalid suspension pages')
        if total is not None and (total!=count or pages!=page_count):raise ValueError('suspension pages changed')
        total,pages=count,page_count
        part=body.get('data')
        if not isinstance(part,list) or (not part and count):raise ValueError('suspension page ended early')
        rows.extend(part)
        if page>=pages:break
    if len(rows)!=total:raise ValueError('suspension rows differ from source count')
    # The service can return intervals starting after the requested day. Exact
    # start/end/resume times, rather than the request filter, prove coverage.
    normalized=full_session(rows,day)
    unique={}
    for row in normalized:
        if row['symbol'] not in unique:unique[row['symbol']]=row
        else:unique[row['symbol']].setdefault('additional_intervals',[]).append({'start':row['start'],'end':row['end']})
    return list(unique.values())


def fetch(day):
    import akshare as ak
    import re
    from .normalize import PendingData
    frame = ak.stock_tfp_em(date=day.replace("-",""))
    if frame.empty:
        raise PendingData("empty AKShare suspension report is unverified")
    rows = json.loads(frame.to_json(orient="records",date_format="iso"))
    result = []
    for row in rows:
        from .normalize import day_string
        begins = day_string(row["停牌时间"])
        resume = day_string(row["预计复牌时间"]) if row.get("预计复牌时间") else None
        ends = day_string(row["停牌截止时间"]) if row.get("停牌截止时间") else None
        symbol = str(row["代码"]).zfill(6)
        if not re.fullmatch(r"\d{6}",symbol):
            raise ValueError("invalid suspension symbol")
        # AKShare reduces timestamps to dates. Same-day intraday pauses remain
        # unverified unless the source explicitly says the whole session.
        explicit = str(row["停牌期限"]).strip() in ("全天","一天","1天")
        full = (begins < day and (ends is None or ends > day)) or explicit
        if full and begins <= day and (resume is None or resume > day):
            result.append(dict(key=symbol,symbol=symbol,name=row["名称"],trade_date=day,
                          status="reported_full_session_suspension",source="akshare/stock_tfp_em",
                          reason=row["停牌原因"],start=begins,end=row.get("停牌截止时间")))
    return result


def saved(day):
    with db.connect() as con:
        return {r["symbol"]:json.loads(r["payload"]) for r in con.execute("SELECT * FROM datasets WHERE dataset='suspensions' AND trade_date=?",(day,))}
