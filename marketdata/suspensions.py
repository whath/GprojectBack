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
    import requests
    from .normalize import PendingData
    params={"reportName":"RPT_CUSTOM_SUSPEND_DATA_INTERFACE","columns":"ALL","pageSize":"500","pageNumber":"1",
            "filter":f'(MARKET="全部")(DATETIME=\'{day}\')'}
    rows=[]
    for page in range(1,11):
        params["pageNumber"]=str(page)
        response=requests.get("https://datacenter-web.eastmoney.com/api/data/v1/get",params=params,timeout=15)
        response.raise_for_status();payload=response.json()
        if payload.get("success") is not True or not payload.get("result"):
            raise PendingData("empty suspension report; no-record state unverified")
        result=payload["result"];rows.extend(result["data"])
        if page>=result["pages"]:
            if len(rows)!=result["count"]:raise ValueError("incomplete suspension pagination")
            return full_session(rows,day)
    raise ValueError("suspension pagination exceeded bound")


def saved(day):
    with db.connect() as con:
        return {r["symbol"]:json.loads(r["payload"]) for r in con.execute("SELECT * FROM datasets WHERE dataset='suspensions' AND trade_date=?",(day,))}
