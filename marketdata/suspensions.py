"""Verify full-session suspension; intraday pauses and resumed stocks do not qualify."""
import json
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
            result.append({"key":symbol,"symbol":symbol,"name":row["SECURITY_NAME_ABBR"],
                           "trade_date":day,"status":"reported_full_session_suspension",
                           "source":"eastmoney/suspension_report","reason":row["SUSPEND_REASON"],
                           "start":row["SUSPEND_START_TIME"],"end":row.get("SUSPEND_END_TIME")})
        except (KeyError,TypeError,ValueError):
            raise ValueError("invalid suspension report") from None
    return result


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
