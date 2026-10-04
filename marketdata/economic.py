"""AKShare economic calendar facts with explicit missing event provenance."""
import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from . import db
from .calendars import CN
from .funds import aware

LIMITATIONS = ["source does not expose publication timestamp", "no individual original-event URL",
               "source clock timezone and numeric units are unverified",
               "not a verified scheduled-event feed; /v1/events remains separate"]


def normalize(rows, target):
    items={}
    for row in rows:
        day=str(row["日期"])[:10]
        if day != target:
            raise ValueError("economic calendar date mismatch")
        title=str(row["事件"]).strip()
        if not title:
            raise ValueError("missing economic title")
        clock=str(row.get("时间", "")).strip()
        occurs=None
        if clock and clock not in ("待定","全天","--"):
            datetime.fromisoformat(day+"T"+clock)  # Validate, retain source clock without inventing a zone.
        region=str(row.get("国家") or row.get("地区") or "").strip()
        markets={"美国":["US"],"日本":["JP"],"韩国":["KR"]}.get(region,[])
        period=str(row.get("统计周期") or "")
        identifier=hashlib.sha256(json.dumps([day,region,title,period],ensure_ascii=False).encode()).hexdigest()
        def value(column):
            val=row.get(column)
            if val is None:return None
            if isinstance(val,bool) or not isinstance(val,(int,float)) or not math.isfinite(val):
                raise ValueError("invalid economic numeric value")
            return val
        item=dict(id=identifier,trade_date=day,title=title,region=region,markets=markets,
                  occurs_at=occurs,source_clock=clock,source_timezone=None,unit=None,
                  period=period,actual=value("公布"),previous=value("前值"),
                  expected=value("预期"),source_importance=value("重要性"),published_at=None,original_url=None,
                  source="akshare/news_economic_baidu",source_page="https://finance.baidu.com/calendar")
        if identifier in items and items[identifier]!=item:
            raise ValueError("conflicting calendar identity")
        items[identifier]=item
    if not items:
        raise ValueError("empty economic calendar response is unverified")
    return sorted(items.values(),key=lambda r:(r["source_clock"] or "",r["id"]))


def collect(provider, target):
    items=normalize(provider.call("news_economic_baidu",date=target.replace("-","")),target)
    key="economic:"+target
    verified=db.now_iso()
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        saved=con.execute("SELECT payload FROM feed_snapshots WHERE feed_key=?",(key,)).fetchone()
        previous=json.loads(saved[0]) if saved else {}
        old={r["id"]:r for r in previous.get("items",[])}
        for row in items:
            prior=old.get(row["id"])
            row["updated_at"]=prior["updated_at"] if prior and all(prior.get(k)==v for k,v in row.items()) else verified
        # An authoritative fetched date replaces its old set; removed agenda rows
        # are visible as removed_ids without pretending to know a cancellation reason.
        removed=sorted(set(old)-{r["id"] for r in items})
        changed=bool(removed) or any(r["updated_at"]==verified for r in items)
        payload=dict(trade_date=target,items=items,removed_ids=removed,
                     updated_at=verified if changed else previous.get("updated_at",verified),
                     verified_at=verified,coverage="economic_facts_with_missing_publication_provenance",limitations=LIMITATIONS)
        con.execute("INSERT OR REPLACE INTO feed_snapshots VALUES(?,?,?,'complete',NULL)",
                    (key,json.dumps(payload,ensure_ascii=False,allow_nan=False),verified))
    return len(items)


def report(target, limit):
    with db.connect() as con:
        saved=con.execute("SELECT * FROM feed_snapshots WHERE feed_key=?",("economic:"+target,)).fetchone()
    if not saved:raise HTTPException(501,"AKShare economic calendar snapshot unavailable")
    if saved["status"]!="complete":raise HTTPException(503,"economic calendar collection failed")
    try:
        payload=json.loads(saved["payload"])
        if payload["trade_date"]!=target or len({r["id"] for r in payload["items"]})!=len(payload["items"]):
            raise ValueError("invalid economic snapshot")
        for row in payload["items"]:
            if row["source"]!="akshare/news_economic_baidu" or row["trade_date"]!=target:
                raise ValueError("invalid economic provenance")
            for key in ("actual","previous","expected","source_importance"):
                if row[key] is not None and (isinstance(row[key],bool) or not isinstance(row[key],(int,float)) or not math.isfinite(row[key])):
                    raise ValueError("invalid stored economic numeric")
        return dict(payload,total=len(payload["items"]),items=payload["items"][:limit])
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,"stored economic calendar failed validation") from None


def run_slice(cfg):
    from .pipeline import CollectorLock
    from .provider import AKProvider
    now=datetime.now(timezone.utc)
    today=now.astimezone(CN).date()
    days=[(today+timedelta(days=n)).isoformat() for n in range(cfg.get("economic_calendar_days",3))]
    with db.connect() as con:
        states={r["feed_key"]:dict(r) for r in con.execute("SELECT * FROM feed_attempts WHERE feed_key LIKE 'economic:%'")}
    due=[d for d in days if "economic:"+d not in states or
         (now-aware(states["economic:"+d]["last_attempt"])).total_seconds()>=cfg.get("events_interval_seconds",3600)]
    if not due:return
    target=min(due,key=lambda d:states.get("economic:"+d,{}).get("last_attempt",""))
    key="economic:"+target
    try:
        with CollectorLock():
            count=collect(AKProvider(dict(cfg,request_timeout_seconds=20,request_attempts=1)),target)
            db.feed_attempt(key,"complete")
            return count
    except Exception as exc:
        db.feed_attempt(key,"failed",type(exc).__name__)
        with db.connect() as con:
            con.execute("INSERT INTO feed_snapshots VALUES(?,'{}',?,'failed',?) ON CONFLICT(feed_key) DO UPDATE SET status='failed',message=excluded.message",
                        (key,db.now_iso(),type(exc).__name__))
