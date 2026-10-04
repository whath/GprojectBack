"""Validated, source-pinned cash-flow history; queries never fetch the provider."""
import json
import math
import re
from datetime import date, datetime, timezone

from fastapi import HTTPException

from . import db
from .calendars import latest_ready, session_close
from .settings import config


def aware(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp needs timezone")
    return stamp


def validate(payload, identifier):
    stock = bool(re.fullmatch(r"CN\.stock\.\d{6}", identifier))
    board = bool(re.fullmatch(r"CN\.(industry|concept)\.THS_\d{6}", identifier))
    if not stock and not board:
        raise ValueError("unsupported funds instrument")
    key = "instrument_id" if stock else "board_id"
    if payload.get(key) != identifier or payload.get("currency") != "CNY" or payload.get("unit") != "yuan":
        raise ValueError("funds ID/currency/unit mismatch")
    if payload.get("scope") not in (("main",) if stock else ("main", "all")):
        raise ValueError("invalid funds scope")
    if board and payload.get("classification_source") != "ths":
        raise ValueError("wrong board classification")
    if not payload.get("source", "").startswith("akshare/"):
        raise ValueError("AKShare source required")
    if aware(payload["updated_at"]) > datetime.now(timezone.utc):
        raise ValueError("future update timestamp")
    cutoff = latest_ready("CN", config()).isoformat()
    seen, items = set(), []
    for row in payload["items"]:
        day = date.fromisoformat(row["trade_date"])
        if day.isoformat() > cutoff or session_close("CN", day, config()) is None or day in seen:
            raise ValueError("duplicate/future/non-session funds date")
        seen.add(day)
        values = {}
        for name in ("inflow", "outflow", "net"):
            value = row.get(name)
            if value is None:
                if name == "net":
                    raise ValueError("net is required")
            elif isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or (name != "net" and value < 0):
                raise ValueError("invalid funds amount")
            values[name] = value
        if values["inflow"] is not None and values["outflow"] is not None:
            if abs(values["inflow"] - values["outflow"] - values["net"]) > max(1, abs(values["net"]) * 0.0001):
                raise ValueError("inflow/outflow/net conflict")
        if row.get("source", payload["source"]) != payload["source"] or row.get("unit", "yuan") != "yuan":
            raise ValueError("mixed funds provenance")
        items.append(dict(trade_date=day.isoformat(), **values))
    if not items:
        raise ValueError("empty provider funds response is unverified")
    return dict(payload, items=sorted(items, key=lambda r: r["trade_date"]))


def save(payload, identifier):
    payload = validate(payload, identifier)
    key = "flows:" + identifier
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        old = con.execute("SELECT payload FROM feed_snapshots WHERE feed_key=?", (key,)).fetchone()
        if old:
            previous = json.loads(old[0])
            if any(previous[k] != payload[k] for k in ("source", "scope", "currency", "unit")):
                raise ValueError("funds source/scope switch requires separate rebuild")
            if aware(payload["updated_at"]) < aware(previous["updated_at"]):
                raise ValueError("funds update is older than saved snapshot")
            if aware(payload["updated_at"]) == aware(previous["updated_at"]):
                known = {r["trade_date"]: r for r in previous["items"]}
                if any(r["trade_date"] in known and known[r["trade_date"]] != r for r in payload["items"]):
                    raise ValueError("conflicting funds version")
            rows = {r["trade_date"]: r for r in previous["items"]}
            rows.update({r["trade_date"]: r for r in payload["items"]})
            payload["items"] = [rows[d] for d in sorted(rows)]
        con.execute("INSERT OR REPLACE INTO feed_snapshots VALUES(?,?,?,'complete',NULL)",
                    (key, json.dumps(payload, allow_nan=False), db.now_iso()))
    return len(payload["items"])


def failed(identifier, message):
    with db.connect() as con:
        con.execute("""INSERT INTO feed_snapshots VALUES(?,'{}',?,'failed',?)
          ON CONFLICT(feed_key) DO UPDATE SET status='failed',message=excluded.message""",
                    ("flows:" + identifier, db.now_iso(), message))


def report(identifier, limit, end=None):
    with db.connect() as con:
        row = con.execute("SELECT * FROM feed_snapshots WHERE feed_key=?", ("flows:" + identifier,)).fetchone()
    if row is None:
        raise HTTPException(501, "verified AKShare funds snapshot not yet available")
    if row["status"] != "complete":
        raise HTTPException(503, "funds collection failed; retry later")
    try:
        payload = validate(json.loads(row["payload"]), identifier)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, "stored funds failed validation; repair required") from None
    cutoff = min(end.isoformat() if end else latest_ready("CN", config()).isoformat(), latest_ready("CN", config()).isoformat())
    payload["items"] = [r for r in payload["items"] if r["trade_date"] <= cutoff][-limit:]
    if "board_id" in payload:
        from .history import sessions
        payload["trade_dates"] = sessions("CN", date.fromisoformat(cutoff), limit, config())
        payload["items"] = [r for r in payload["items"] if r["trade_date"] in payload["trade_dates"]]
    payload["latest_date"] = max((r["trade_date"] for r in payload["items"]), default=None)
    payload["stale"] = payload["latest_date"] != cutoff
    return payload


def collect_stock(collector, item):
    symbol = item["symbol"]
    market = "sh" if symbol.startswith("6") else "sz" if symbol.startswith(("0", "3")) else "bj"
    rows = collector.call("stock_individual_fund_flow", stock=symbol, market=market)
    for row in rows:
        if row.get("大单净流入-净额") is not None and row.get("超大单净流入-净额") is not None:
            main, large, super_large = (row[k] for k in ("主力净流入-净额", "大单净流入-净额", "超大单净流入-净额"))
            if any(v is None or isinstance(v, bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
                   for v in (main, large, super_large)) or abs(main-large-super_large) > max(1,abs(main)*0.0001):
                raise ValueError("provider main-order definition mismatch")
    payload = {"instrument_id": item["id"], "currency": "CNY", "unit": "yuan", "scope": "main",
               "source": "akshare/eastmoney/main-order-flow/v1", "updated_at": db.now_iso(),
               "definition": "provider main = large + super-large orders; recent history only",
               "items": [{"trade_date": str(r["日期"])[:10], "inflow": None, "outflow": None,
                          "net": r["主力净流入-净额"]} for r in rows if str(r["日期"])[:10] <= collector.day.isoformat()]}
    try:
        count = save(payload, item["id"])
        if not payload["items"] or max(r["trade_date"] for r in payload["items"]) != collector.day.isoformat():
            from .normalize import PendingData
            raise PendingData("target funds date absent; historical snapshot retained")
        return count
    except Exception as exc:
        failed(item["id"], str(exc))
        raise


def run_slice(cfg, day=None, symbols=None):
    from .pipeline import Collector, CollectorLock
    from .normalize import PendingData
    import time
    day = day or latest_ready("CN", cfg)
    with db.connect() as con:
        items = [dict(r) for r in con.execute("SELECT i.* FROM instruments i LEFT JOIN instrument_lifecycle l ON l.instrument_id=i.id WHERE i.market='CN' AND i.kind='stock' AND COALESCE(l.status,'listed')='listed' ORDER BY i.id")]
        states = {r["task_key"]: dict(r) for r in con.execute("SELECT * FROM tasks WHERE market='CN' AND scope='funds' AND trade_date=?", (day.isoformat(),))}
    items = [i for i in items if symbols is None or i["symbol"] in symbols]
    items.sort(key=lambda i: (states.get("flows:" + i["id"], {}).get("attempts", 0), i["id"]))
    collector = Collector("CN", day, settings=dict(cfg,collection_budget_seconds=60,request_attempts=1,request_timeout_seconds=20,
                          akshare_transport=cfg.get("funds_transport", "curl_cffi")))
    collector.scope = "funds"
    with CollectorLock():
        attempted = 0
        for item in items:
            key = "flows:" + item["id"]
            state = states.get(key)
            if state and state["attempts"] >= cfg.get("funds_max_attempts",3):
                continue
            if state and (datetime.now(timezone.utc) - aware(state["updated_at"])).total_seconds() < cfg.get("funds_refresh_seconds",3600):
                continue
            if time.monotonic() - collector.started >= 60:
                break
            def action(item=item):
                try:
                    result=collect_stock(collector, item)
                    db.feed_attempt("flows:"+item["id"],"complete")
                    return result
                except Exception as exc:
                    failed(item["id"], str(exc))
                    db.feed_attempt("flows:"+item["id"],"failed",str(exc))
                    raise
            collector.task(key, action,always=bool(state and state["status"]=="complete"))
            attempted += 1
    with db.connect() as con:
        states=[dict(r) for r in con.execute("SELECT task_key,status FROM tasks WHERE market='CN' AND trade_date=? AND scope='funds'",(day.isoformat(),))]
    keys={"flows:"+i["id"] for i in items}
    complete={s["task_key"] for s in states if s["status"]=="complete"}
    status="complete" if keys and keys<=complete else "partial"
    return {"trade_date":day.isoformat(),"attempted":attempted,"scope":"funds","status":status,
            "expected":len(keys),"complete_count":len(keys&complete)}
