import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .analytics import us_report
from .calendars import latest_ready, session_close
from .settings import config
from .health import health as operational_health,coverage
from .operations import report_tasks
from .snapshots import page as snapshot_page

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


app=FastAPI(title="Personal EOD Market API",version="0.4.1",lifespan=lifespan,
            description="收盘数据服务；CN 交易日北京时间 15:10、17:10、19:10 三轮采集；美股行业 ETF 代理排名和可编辑 AI 观察池。")

if config().get("cors_origins"):
    app.add_middleware(CORSMiddleware,allow_origins=config()["cors_origins"],allow_methods=["GET"],
                       allow_headers=["Authorization","Content-Type"],allow_credentials=False)


@app.middleware("http")
async def private_cache(request,call_next):
    try:
        response=await call_next(request)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        response=JSONResponse({"detail":"database busy; retry later"},status_code=503,headers={"Retry-After":"5"})
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
          scope:Literal["sample","configured","full_history","requested_history","funds","lifecycle"]="configured",limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
    day=chosen_day(market,trade_date)
    rows=report_tasks(market,day,scope)[offset:offset+limit]
    return {"trade_date":day,"scope":scope,"items":rows}


@app.get("/v1/instruments",dependencies=[Depends(authorize)])
def instruments(request:Request,market:Literal["CN","US"]|None=None,kind:Literal["stock","etf","industry","concept"]|None=None,
                search:str=Query("",max_length=80),limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),
                refresh:bool=False,snapshot_id:str|None=Query(None,pattern=r"^[a-f0-9]{64}$")):
    clauses=["(i.symbol LIKE ? OR i.name LIKE ?)","COALESCE(l.status,'listed')='listed'"]
    args=[f"%{search}%",f"%{search}%"]
    for field,value in (("market",market),("kind",kind)):
        if value:
            clauses.append("i."+field+"=?")
            args.append(value)
    if kind in ("industry","concept") and market == "CN":
        board_source=config().get("cn_sources",{}).get("boards","eastmoney")
        clauses.append("i.symbol LIKE ?")
        args.append("THS_%" if board_source=="ths" else "BK%")
    where=" AND ".join(clauses)
    def load(con):
        rows=[dict(r) for r in con.execute("SELECT i.*,l.status AS listing_status,l.listed_date,l.delisted_date FROM instruments i LEFT JOIN instrument_lifecycle l ON l.instrument_id=i.id WHERE "+where+" ORDER BY i.id",args)]
        try:
            for row in rows:
                db.validate_instrument(row)
        except ValueError:
            raise HTTPException(503,"stored catalog failed validation; repair required") from None
        if not rows and not search:
            states=con.execute("SELECT status FROM tasks WHERE (? IS NULL OR market=?) AND task_key IN (?,?) ORDER BY trade_date DESC",
                               (market,market,"catalog:"+(kind or "stock"),"catalog:us")).fetchall()
            if states and states[0][0]=="failed":
                raise HTTPException(503,"catalog collection failed; retry later")
        return {"total":len(rows),"items":rows}
    return snapshot_page(["instruments",market,kind,search],offset,limit,load,
                         request.headers.get("authorization",""),refresh,snapshot_id)


@app.get("/v1/bars/{instrument_id}",dependencies=[Depends(authorize)])
def bars(instrument_id:str,start:date|None=None,end:date|None=None,limit:int=Query(500,ge=1,le=2000)):
    if start and end and start>end:
        raise HTTPException(422,"start must not follow end")
    with db.connect() as con:
        item=con.execute("SELECT * FROM instruments WHERE id=?",(instrument_id,)).fetchone()
        if not item:
            raise HTTPException(404,"unknown instrument")
        cutoff=min(end.isoformat() if end else chosen_day(item["market"],None),chosen_day(item["market"],None))
        rows=[dict(r) for r in con.execute('''SELECT * FROM bars WHERE instrument_id=? AND trade_date>=?
          AND trade_date<=? AND adjustment='none' ORDER BY trade_date DESC LIMIT ?''',
          (instrument_id,(start or date(1900,1,1)).isoformat(),cutoff,limit))]
        states=[dict(r) for r in con.execute("SELECT status,message,updated_at FROM tasks WHERE task_key=? AND trade_date<=? ORDER BY trade_date DESC",("bar:"+instrument_id,cutoff))]
    if not rows and states and states[0]["status"]=="failed":
        raise HTTPException(503,"history collection failed; retry later")
    try:
        db.validate_instrument(dict(item))
        db.validate_bars(instrument_id,rows)
    except ValueError:
        raise HTTPException(503,"stored history failed validation; repair required") from None
    coverage_info=None
    if not start:
        from .history import audit
        try:
            coverage_info=audit(instrument_id,cutoff,config(),limit)
        except ValueError:
            raise HTTPException(422,"date is outside the supported calendar range") from None
        if coverage_info["missing_dates"]:
            requested_start=coverage_info["missing_dates"][0]
            with db.connect() as con:
                con.execute('''INSERT INTO history_requests VALUES(?,?,?,?) ON CONFLICT(instrument_id)
                  DO UPDATE SET start_date=min(history_requests.start_date,excluded.start_date),
                  target_date=max(history_requests.target_date,excluded.target_date),updated_at=excluded.updated_at
                  WHERE excluded.start_date<history_requests.start_date OR excluded.target_date>history_requests.target_date''',
                  (instrument_id,requested_start,cutoff,db.now_iso()))
    return {"instrument":dict(item),"adjustment":"none","items":list(reversed(rows)),"task_status":states[:1],
            "history_coverage":coverage_info}


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
    if not rows and any(r["status"]=="failed" for r in counts):
        raise HTTPException(503,"ranking collection failed; retry later")
    return {"trade_date":day,"items":rows,"task_coverage":counts,"scope":"collected_instruments_only",
            "ranking_metric":metric,"previous_session":previous.isoformat(),
            "classification_source":classification_source if board_filter else None,
            "return_basis":"unadjusted_close_price; not exchange reference-price return; corporate actions may distort" if metric=="price_change_pct" else "source_reported_change_pct"}


def records(dataset,day,symbol,limit,offset,connection=None):
    task_key = dataset.replace("lhb_seats:", "seats:", 1) if dataset.startswith("lhb_seats:") else dataset
    from contextlib import nullcontext
    with nullcontext(connection) if connection is not None else db.connect() as con:
        rows=con.execute('''SELECT payload,collected_at FROM datasets WHERE dataset=? AND market='CN'
          AND trade_date=? AND (? IS NULL OR symbol=?) ORDER BY record_key LIMIT ? OFFSET ?''',
          (dataset,day,symbol,symbol,limit,offset)).fetchall()
        states=[dict(r) for r in con.execute('''SELECT scope,status,message,updated_at FROM tasks
          WHERE market='CN' AND trade_date=? AND task_key=? ORDER BY updated_at DESC LIMIT 1''',(day,task_key))]
    if not rows and any(s["status"]=="failed" for s in states):
        raise HTTPException(503,"dataset collection failed; retry later")
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
def board_members(request:Request,instrument_id:str,observed_date:date,limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),refresh:bool=False,
                  snapshot_id:str|None=Query(None,pattern=r"^[a-f0-9]{64}$")):
    import re
    if not re.fullmatch(r"CN\.(industry|concept)\.(THS_\d{6}|BK[A-Z0-9]+)",instrument_id):
        raise HTTPException(422,"board ID required")
    from .calendars import CN
    from datetime import datetime
    if observed_date > datetime.now(CN).date():
        raise HTTPException(422,"future membership observation")
    def load(con):
        data=records("members:"+instrument_id,observed_date.isoformat(),None,100000,0,con)
        if not data["items"] and ".THS_" in instrument_id:
            raise HTTPException(501,"AKShare 1.19.1 has no verified complete THS membership source")
        from .normalize import members
        try:
            if data["items"]:
                members([{"代码":r["symbol"],"名称":r["name"]} for r in data["items"]])
            counts = [r["row_count"] for r in con.execute("SELECT row_count,status FROM tasks WHERE market='CN' AND trade_date=? AND task_key=? ORDER BY updated_at DESC LIMIT 1",
                                              (observed_date.isoformat(),"members:"+instrument_id)) if r["status"]=="complete"]
            if counts and any(n != len(data["items"]) for n in counts):
                raise ValueError("member count differs from complete task")
        except (ValueError, KeyError, TypeError):
            raise HTTPException(503,"stored membership failed validation; repair required") from None
        return data
    with db.connect() as con:
        states=[r[0] for r in con.execute("SELECT status FROM tasks WHERE market='CN' AND trade_date=? AND task_key=? ORDER BY updated_at DESC LIMIT 1",
                                        (observed_date.isoformat(),"members:"+instrument_id))]
    incomplete=not states or any(s!="complete" for s in states)
    return snapshot_page(["members",instrument_id,observed_date.isoformat()],offset,limit,load,
                         request.headers.get("authorization",""),refresh or (incomplete and not snapshot_id),snapshot_id)


@app.get("/v1/cn/boards/{board_id}/flows",dependencies=[Depends(authorize)])
def board_flows(board_id:str,limit:int=Query(20,ge=1,le=20),classification_source:Literal["ths"]="ths"):
    import re
    if not re.fullmatch(r"CN\.(industry|concept)\.THS_\d{6}",board_id):
        raise HTTPException(422,"THS board ID required")
    from .funds import report
    return report(board_id,limit)


@app.get("/v1/cn/stocks/{instrument_id}/flows",dependencies=[Depends(authorize)])
def stock_flows(instrument_id:str,end:date|None=None,limit:int=Query(2000,ge=1,le=2000)):
    import re
    if not re.fullmatch(r"CN\.stock\.\d{6}",instrument_id):
        raise HTTPException(422,"CN stock ID required")
    from .funds import report
    return report(instrument_id,limit,end)


@app.get("/v1/history-coverage/{instrument_id}",dependencies=[Depends(authorize)])
def instrument_history_coverage(instrument_id:str,end:date|None=None,limit:int=Query(250,ge=1,le=2000)):
    with db.connect() as con:
        item=con.execute("SELECT market FROM instruments WHERE id=?",(instrument_id,)).fetchone()
    if not item:
        raise HTTPException(404,"unknown instrument")
    from .history import audit
    day=min(chosen_day(item["market"],end),chosen_day(item["market"],None))
    try:
        return audit(instrument_id,day,config(),limit)
    except ValueError:
        raise HTTPException(422,"date is outside the supported calendar range") from None


@app.get("/v1/events",dependencies=[Depends(authorize)])
def events(limit:int=Query(100,ge=1,le=100)):
    from .events import report
    return report(limit)


@app.get("/v1/economic-calendar",dependencies=[Depends(authorize)])
def economic_calendar(trade_date:date,limit:int=Query(100,ge=1,le=500)):
    from .economic import report
    return report(trade_date.isoformat(),limit)


@app.get("/v1/capabilities",dependencies=[Depends(authorize)])
def capabilities():
    from importlib.metadata import version
    return {"provider":"akshare","version":version("akshare"),
            "stock_funds":{"implemented":True,"source":"stock_individual_fund_flow",
                           "transport":config().get("funds_transport","curl_cffi"),
                           "historical_limit":"provider recent window (documented ~100; 120 observed); stored history accumulates"},
            "ths_members":{"implemented":False,"reason":"no verified complete function in pinned AKShare"},
            "ths_board_funds":{"implemented":False,"reason":"public industry table lacks dated daily history and required amount precision"},
            "events":{"news":True,"scheduled_calendar":False,"coverage":"configured keyword search only",
                      "economic_facts":True,"economic_endpoint":"/v1/economic-calendar"}}


@app.get("/v1/catalog-coverage",dependencies=[Depends(authorize)])
def catalog_coverage():
    from .lifecycle import report
    return report()


@app.get("/v1/deployment-status",dependencies=[Depends(authorize)])
def deployment_status():
    from pathlib import Path
    path=Path(os.getenv("DEPLOYMENT_STATUS_PATH", "config/deployment-status.json"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError):
        raise HTTPException(503,"deployment status unavailable") from None


@app.get("/v1/feed-status",dependencies=[Depends(authorize)])
def feed_status():
    with db.connect() as con:
        rows=[dict(r) for r in con.execute("SELECT * FROM feed_attempts ORDER BY feed_key")]
        snapshots=[dict(r) for r in con.execute("SELECT feed_key,verified_at,status,message FROM feed_snapshots ORDER BY feed_key")]
    return {"items":rows,"snapshots":snapshots}


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
    def independent(action):
        try:
            return action()
        except HTTPException as exc:
            return {"status":"failed","http_status":exc.status_code,"message":exc.detail,"items":[]}
    return {"generated_at":db.now_iso(),"health":operational_health(),
            "cn":{"coverage":coverage("CN",cn_day,cfg),
                  "close_quotes":{k:quote_report(cn_day,k,0) for k in ("stock","etf")},
                  "full_history":history_report(cn_day),
                  "industries":independent(lambda:rankings("industry",date.fromisoformat(cn_day),10,"price_change_pct",None)),
                  "concepts":independent(lambda:rankings("concept",date.fromisoformat(cn_day),10,"price_change_pct",None)),
                  "lhb":independent(lambda:records("lhb",cn_day,None,10,0))},
            "us":{"coverage":coverage("US",us_day,cfg),"sectors":us_report(us_day,cfg),"ai":us_report(us_day,cfg,ai=True)}}
