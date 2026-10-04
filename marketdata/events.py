"""AKShare news search with explicit market mappings and versioned events."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import HTTPException

from . import db
from .calendars import CN
from .funds import aware

MARKETS = {"US", "JP", "KR", "OIL", "GOLD"}


def validate(payload):
    updated = aware(payload["updated_at"])
    now = datetime.now(timezone.utc)
    if updated > now:
        raise ValueError("future feed update")
    items = {}
    for row in payload["items"]:
        if not row.get("id") or not row.get("title") or not row.get("summary") or not row.get("source"):
            raise ValueError("missing event identity/content/provenance")
        if not row.get("markets") or not set(row["markets"]) <= MARKETS:
            raise ValueError("unsupported event markets")
        if row.get("importance") not in ("high", "normal") or row.get("kind") not in ("scheduled", "news"):
            raise ValueError("invalid event category")
        link = urlparse(row["url"])
        if link.scheme != "https" or not link.hostname or link.username or link.password:
            raise ValueError("HTTPS source URL required")
        published, expires, revision, occurs = (aware(row[k]) for k in ("published_at", "expires_at", "updated_at", "occurs_at"))
        if expires <= published or revision < published or revision > now or revision > updated:
            raise ValueError("invalid event version/times")
        old = items.get(row["id"])
        if old is None or aware(old["updated_at"]) < revision:
            items[row["id"]] = row
        elif aware(old["updated_at"]) == revision and old != row:
            raise ValueError("conflicting event version")
    return dict(payload, items=list(items.values()))


def save(payload):
    payload = validate(payload)
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        saved=con.execute("SELECT payload FROM feed_snapshots WHERE feed_key='events'").fetchone()
        previous=json.loads(saved[0]) if saved else {}
        if previous.get("updated_at") and aware(previous["updated_at"])>aware(payload["updated_at"]):
            raise ValueError("event feed update moved backwards")
        merged={r["id"]:r for r in previous.get("items",[])}
        for row in payload["items"]:
            old=merged.get(row["id"])
            if old and aware(old["updated_at"])>aware(row["updated_at"]):
                continue
            if old and aware(old["updated_at"])==aware(row["updated_at"]) and old!=row:
                raise ValueError("conflicting stored event version")
            merged[row["id"]]=row
        oldest=datetime.now(timezone.utc)-timedelta(days=7)
        payload["items"]=[r for r in merged.values() if aware(r["expires_at"])>oldest]
        con.execute("INSERT OR REPLACE INTO feed_snapshots VALUES('events',?,?, 'complete',NULL)",
                    (json.dumps(payload, ensure_ascii=False, allow_nan=False), db.now_iso()))
    return len(payload["items"])


def report(limit, now=None):
    now = now or datetime.now(timezone.utc)
    with db.connect() as con:
        saved = con.execute("SELECT * FROM feed_snapshots WHERE feed_key='events'").fetchone()
    if saved is None:
        raise HTTPException(501, "verified AKShare events not yet available")
    if saved["status"] != "complete":
        raise HTTPException(503, "events collection failed; retry later")
    try:
        payload = validate(json.loads(saved["payload"]))
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, "stored events failed validation; repair required") from None
    items = [r for r in payload["items"] if aware(r["published_at"]) <= now < aware(r["expires_at"])]
    items.sort(key=lambda r: (0 if r["importance"] == "high" else 1, aware(r["occurs_at"]), r["id"]))
    return dict(payload, items=items[:limit])


def collect(provider, queries, now=None):
    """The query mapping defines markets; do not infer them from titles."""
    now = now or datetime.now(timezone.utc)
    if not queries:
        raise ValueError("event search queries not configured")
    with db.connect() as con:
        saved = con.execute("SELECT payload FROM feed_snapshots WHERE feed_key='events'").fetchone()
    previous = {r["id"]: r for r in json.loads(saved[0]).get("items", [])} if saved else {}
    results = {}
    source_dates = []
    for query in queries:
        markets = query["markets"]
        if not markets or not set(markets) <= MARKETS:
            raise ValueError("invalid configured event markets")
        rows = provider.call("stock_news_em", symbol=query["keyword"])
        if not rows:
            raise ValueError("empty AKShare news response is unverified")
        for row in rows:
            published = datetime.fromisoformat(str(row["发布时间"]))
            if published.tzinfo is None:
                published = published.replace(tzinfo=CN)
            if published > now:
                raise ValueError("future provider news date")
            source_dates.append(published)
            link = str(row["新闻链接"])
            # AKShare constructs HTTP URLs for this HTTPS-capable canonical publisher.
            if link.startswith("http://finance.eastmoney.com/a/"):
                link = "https://" + link[len("http://"):]
            if not link.startswith("https://"):
                raise ValueError("unverified news source link")
            identifier = hashlib.sha256(link.encode()).hexdigest()
            content = re.sub(r"<[^>]*>", "", str(row["新闻内容"])).strip()
            item = {"id": identifier, "title": str(row["新闻标题"]).strip(),
                    "summary": content[:300], "markets": sorted(set(markets)),
                    "importance": query.get("importance", "normal"), "kind": "news",
                    "occurs_at": published.isoformat(), "published_at": published.isoformat(),
                    "expires_at": (published + timedelta(hours=query.get("valid_hours", 48))).isoformat(),
                    "updated_at": published.isoformat(), "source": str(row["文章来源"]), "url": link}
            if identifier in results:
                if any(item[k] != results[identifier][k] for k in ("title", "summary", "published_at", "source")):
                    raise ValueError("conflicting news identity across queries")
                item["markets"] = sorted(set(item["markets"]) | set(results[identifier]["markets"]))
                if results[identifier]["importance"] == "high":
                    item["importance"] = "high"
            results[identifier] = item
    for identifier, item in results.items():
        old = previous.get(identifier)
        if old:
            # A keyword may roll off the search's ten-item window. Preserve verified
            # associations; an explicit correction can still use the versioned save path.
            item["markets"] = sorted(set(item["markets"]) | set(old["markets"]))
            if all(old[k] == item[k] for k in item if k != "updated_at"):
                item["updated_at"] = old["updated_at"]
            else:
                item["updated_at"] = now.isoformat()
    if not source_dates:
        raise ValueError("no verified news publication dates")
    # Old source content keeps an old feed time even after a successful HTTP fetch.
    feed_time = max([max(source_dates)] + [aware(r["updated_at"]) for r in results.values()])
    return save({"updated_at": feed_time.isoformat(), "items": list(results.values()),
                 "provider": "akshare/stock_news_em", "coverage": "configured_keyword_news_only; not exhaustive; no scheduled-event calendar"})


def run_slice(cfg):
    now = datetime.now(timezone.utc)
    with db.connect() as con:
        last = con.execute("SELECT * FROM feed_attempts WHERE feed_key='events'").fetchone()
    if last and (now - aware(last["last_attempt"])).total_seconds() < cfg.get("events_interval_seconds", 3600):
        return
    from .provider import AKProvider
    from .pipeline import CollectorLock
    try:
        with CollectorLock():
            result=collect(AKProvider(dict(cfg, request_timeout_seconds=20, request_attempts=1)), cfg.get("event_queries", []), now)
            db.feed_attempt("events","complete")
            return result
    except Exception as exc:
        message=f"{type(exc).__name__}: {str(exc)[:240]}"
        db.feed_attempt("events","failed",message)
        with db.connect() as con:
            con.execute("""INSERT INTO feed_snapshots VALUES('events','{}',?,'failed',?)
              ON CONFLICT(feed_key) DO UPDATE SET status='failed',message=excluded.message""",
                        (db.now_iso(), message))
