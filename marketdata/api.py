import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .analytics import us_report
from .calendars import latest_ready, session_close
from .settings import config
from .health import health as operational_health,coverage
from .operations import report_tasks

bearer = HTTPBearer(auto_error=False)


def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token=os.getenv("API_TOKEN", "")
    if not token or not credentials or not secrets.compare_digest(credentials.credentials,token):
        raise HTTPException(401,"Bearer token required",headers={"WWW-Authenticate":"Bearer"})


@asynccontextmanager
async def lifespan(app):
    if len(os.getenv("API_TOKEN", ""))<32:
        raise RuntimeError("API_TOKEN must contain at least 32 characters")
    db.init()
    yield


app=FastAPI(title="Personal EOD Market API",version="0.3.0",lifespan=lifespan,
            description="收盘数据服务；CN 交易日北京时间 15:10、17:10、19:10 三轮采集；美股行业 ETF 代理排名和可编辑 AI 观察池。")

if config().get("cors_origins"):
    app.add_middleware(CORSMiddleware,allow_origins=config()["cors_origins"],allow_methods=["GET"],
                       allow_headers=["Authorization","Content-Type"],allow_credentials=False)


@app.middleware("http")
async def private_cache(request,call_next):
    response=await call_next(request)
    response.headers["Cache-Control"]="no-store"
    response.headers["X-Content-Type-Options"]="nosniff"
    return response


@app.get("/healthz")
def health():
    return {"status":"ok"}


@app.get("/readyz")
def ready():
    try:
        state=operational_health()
        return JSONResponse({"ready":state["ready"]},status_code=200 if state["ready"] else 503)
    except Exception:
        return JSONResponse({"ready":False},status_code=503)


@app.get("/v1/health",dependencies=[Depends(authorize)])
def health_details():
    return operational_health()


@app.get("/v1/alerts",dependencies=[Depends(authorize)])
def alerts(include_resolved:bool=False,limit:int=Query(100,ge=1,le=500)):
    with db.connect() as con:
        rows=[dict(r) for r in con.execute("""SELECT * FROM operational_alerts WHERE (? OR resolved_at IS NULL)
          ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,last_seen DESC LIMIT ?""",(include_resolved,limit))]
        counts=dict(con.execute("SELECT severity,count(*) FROM operational_alerts WHERE resolved_at IS NULL GROUP BY severity"))
    return {"items":rows,"unresolved_counts":counts,"health_issues":operational_health()["issues"],"delivery":"dashboard_only"}


@app.get("/v1/coverage",dependencies=[Depends(authorize)])
def coverage_details(market:Literal["CN","US"],trade_date:date|None=None):
    return coverage(market,chosen_day(market,trade_date),config())


def chosen_day(market, day):
    return day.isoformat() if day else latest_ready(market,config()).isoformat()


@app.get("/v1/status",dependencies=[Depends(authorize)])
def status():
    with db.connect() as con:
        runs=[dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20")]
        latest=[dict(r) for r in con.execute('''SELECT i.market,max(b.trade_date) AS latest_bar_date
          FROM bars b JOIN instruments i ON i.id=b.instrument_id GROUP BY i.market''')]
        slots=[dict(r) for r in con.execute("SELECT * FROM schedule_slots ORDER BY updated_at DESC LIMIT 30")]
    return {"runs":runs,"latest_bar_dates":latest,"schedule_slots":slots,
            "note":"sample runs do not certify configured-universe coverage"}


@app.get("/v1/cn/quotes",dependencies=[Depends(authorize)])
def cn_quotes(kind:Literal["stock","etf"]="stock",trade_date:date|None=None,
              limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    from .quotes import report
    return report(chosen_day("CN",trade_date),kind,limit,offset)


@app.get("/v1/cn/history-coverage",dependencies=[Depends(authorize)])
def full_history_coverage(trade_date:date|None=None):
    from .backfill import report
    return report(chosen_day("CN",trade_date))


@app.get("/v1/tasks",dependencies=[Depends(authorize)])
def tasks(market:Literal["CN","US"],trade_date:date|None=None,
          scope:Literal["sample","configured","full_history"]="configured",limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    day=chosen_day(market,trade_date)
    rows=report_tasks(market,day,scope)[offset:offset+limit]
    return {"trade_date":day,"scope":scope,"items":rows}


@app.get("/v1/instruments",dependencies=[Depends(authorize)])
def instruments(market:Literal["CN","US"]|None=None,kind:Literal["stock","etf","industry","concept"]|None=None,
                search:str=Query("",max_length=80),limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    clauses=["(symbol LIKE ? OR name LIKE ?)"]
    args=[f"%{search}%",f"%{search}%"]
    for field,value in (("market",market),("kind",kind)):
        if value:
            clauses.append(field+"=?")
            args.append(value)
    where=" AND ".join(clauses)
    with db.connect() as con:
        total=con.execute("SELECT count(*) FROM instruments WHERE "+where,args).fetchone()[0]
        rows=[dict(r) for r in con.execute("SELECT * FROM instruments WHERE "+where+" ORDER BY id LIMIT ? OFFSET ?",[*args,limit,offset])]
    return {"total":total,"items":rows}


@app.get("/v1/bars/{instrument_id}",dependencies=[Depends(authorize)])
def bars(instrument_id:str,start:date|None=None,end:date|None=None,limit:int=Query(500,ge=1,le=2000)):
    if start and end and start>end:
        raise HTTPException(422,"start must not follow end")
    with db.connect() as con:
        item=con.execute("SELECT * FROM instruments WHERE id=?",(instrument_id,)).fetchone()
        if not item:
            raise HTTPException(404,"unknown instrument")
        rows=[dict(r) for r in con.execute('''SELECT * FROM bars WHERE instrument_id=? AND trade_date>=?
          AND trade_date<=? ORDER BY trade_date DESC LIMIT ?''',
          (instrument_id,(start or date(1900,1,1)).isoformat(),(end or date.max).isoformat(),limit))]
    return {"instrument":dict(item),"adjustment":"none","items":list(reversed(rows))}


@app.get("/v1/cn/rankings",dependencies=[Depends(authorize)])
def rankings(kind:Literal["stock","etf","industry","concept"]="industry",trade_date:date|None=None,limit:int=Query(50,ge=1,le=500),
             metric:Literal["price_change_pct","change_pct"]="price_change_pct",
             classification_source:Literal["ths","eastmoney"]|None=None):
    day=chosen_day("CN",trade_date)
    cfg=config()
    previous=date.fromisoformat(day)-timedelta(days=1)
    for _ in range(31):
        if session_close("CN",previous,cfg) is not None:
            break
        previous-=timedelta(days=1)
    classification_source = classification_source or cfg.get("cn_sources",{}).get("boards","eastmoney")
    board_filter = " AND i.symbol LIKE ?" if kind in ("industry","concept") else ""
    board_args = ["THS_%" if classification_source == "ths" else "BK%"] if board_filter else []
    task_pattern = f"bar:CN.{kind}." + (board_args[0] if board_filter else "%")
    with db.connect() as con:
        # A missing previous MARKET session never becomes a multi-day daily return.
        rows=[dict(r) for r in con.execute(f'''SELECT * FROM (SELECT i.id,i.name,i.symbol,b.*,
          (b.close/p.close-1)*100 AS price_change_pct FROM bars b
          JOIN instruments i ON b.instrument_id=i.id
          LEFT JOIN bars p ON p.instrument_id=b.instrument_id AND p.trade_date=?
            AND p.adjustment=b.adjustment AND p.source=b.source
          WHERE i.market='CN' AND i.kind=?
          AND b.trade_date=?{board_filter}) WHERE {metric} IS NOT NULL ORDER BY {metric} DESC LIMIT ?''',
              (previous.isoformat(),kind,day,*board_args,limit))]
        counts=[dict(r) for r in con.execute('''SELECT scope,status,count(*) AS count FROM tasks
          WHERE market='CN' AND trade_date=? AND task_key LIKE ? GROUP BY scope,status''',(day,task_pattern))]
    return {"trade_date":day,"items":rows,"task_coverage":counts,"scope":"collected_instruments_only",
            "ranking_metric":metric,"previous_session":previous.isoformat(),
            "classification_source":classification_source if board_filter else None,
            "return_basis":"unadjusted_close_price; not exchange reference-price return; corporate actions may distort" if metric=="price_change_pct" else "source_reported_change_pct"}


def records(dataset,day,symbol,limit,offset):
    task_key = dataset.replace("lhb_seats:", "seats:", 1) if dataset.startswith("lhb_seats:") else dataset
    with db.connect() as con:
        rows=con.execute('''SELECT payload,collected_at FROM datasets WHERE dataset=? AND market='CN'
          AND trade_date=? AND (? IS NULL OR symbol=?) ORDER BY record_key LIMIT ? OFFSET ?''',
          (dataset,day,symbol,symbol,limit,offset)).fetchall()
        states=[dict(r) for r in con.execute('''SELECT scope,status,message,updated_at FROM tasks
          WHERE market='CN' AND trade_date=? AND task_key=?''',(day,task_key))]
    return {"trade_date":day,"items":[dict(json.loads(r["payload"]),collected_at=r["collected_at"]) for r in rows],
            "task_status":states,"empty_means":"no verified rows stored; not proof of no disclosures"}


@app.get("/v1/cn/lhb",dependencies=[Depends(authorize)])
def lhb(trade_date:date|None=None,symbol:str|None=Query(None,pattern=r"^\d{6}$"),
        institutions:bool=False,limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    return records("lhb_institutions" if institutions else "lhb",chosen_day("CN",trade_date),symbol,limit,offset)


@app.get("/v1/cn/lhb/{symbol}/seats",dependencies=[Depends(authorize)])
def seat_details(symbol:str,trade_date:date,direction:Literal["买入","卖出"]="买入"):
    return records(f"lhb_seats:{symbol}:{direction}",trade_date.isoformat(),symbol,100,0)


@app.get("/v1/cn/boards/{instrument_id}/members",dependencies=[Depends(authorize)])
def board_members(instrument_id:str,observed_date:date,limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    return records("members:"+instrument_id,observed_date.isoformat(),None,limit,offset)


@app.get("/v1/us/sectors",dependencies=[Depends(authorize)])
def sectors(trade_date:date|None=None):
    return us_report(chosen_day("US",trade_date),config())


@app.get("/v1/us/ai",dependencies=[Depends(authorize)])
def ai_watchlist(trade_date:date|None=None):
    return us_report(chosen_day("US",trade_date),config(),ai=True)


@app.get("/v1/overview",dependencies=[Depends(authorize)])
def overview(cn_date:date|None=None,us_date:date|None=None):
    cfg=config()
    cn_day,us_day=chosen_day("CN",cn_date),chosen_day("US",us_date)
    from .quotes import report as quote_report
    from .backfill import report as history_report
    return {"generated_at":db.now_iso(),"health":operational_health(),
            "cn":{"coverage":coverage("CN",cn_day,cfg),
                  "close_quotes":{k:quote_report(cn_day,k,0) for k in ("stock","etf")},
                  "full_history":history_report(cn_day),
                  "industries":rankings("industry",date.fromisoformat(cn_day),10,"price_change_pct",None),
                  "concepts":rankings("concept",date.fromisoformat(cn_day),10,"price_change_pct",None),
                  "lhb":records("lhb",cn_day,None,10,0)},
            "us":{"coverage":coverage("US",us_day,cfg),"sectors":us_report(us_day,cfg),"ai":us_report(us_day,cfg,ai=True)}}
