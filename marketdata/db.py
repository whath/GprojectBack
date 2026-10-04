import json
import sqlite3
import math
import re
from contextlib import contextmanager,closing
from datetime import datetime, timezone

from .settings import data_dir


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(data_dir() / "market.sqlite3", timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript('''
        CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO schema_version VALUES(1);
        CREATE TABLE IF NOT EXISTS instruments(
          id TEXT PRIMARY KEY, market TEXT NOT NULL, kind TEXT NOT NULL,
          symbol TEXT NOT NULL, name TEXT NOT NULL, source_code TEXT NOT NULL,
          currency TEXT NOT NULL, group_name TEXT, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS instrument_market ON instruments(market,kind);
        CREATE TABLE IF NOT EXISTS bars(
          instrument_id TEXT NOT NULL REFERENCES instruments(id), trade_date TEXT NOT NULL,
          adjustment TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL NOT NULL,
          volume REAL, volume_unit TEXT NOT NULL, amount REAL, change_pct REAL,
          source TEXT NOT NULL, collected_at TEXT NOT NULL,
          PRIMARY KEY(instrument_id,trade_date,adjustment));
        CREATE INDEX IF NOT EXISTS bars_date ON bars(trade_date);
        CREATE TABLE IF NOT EXISTS datasets(
          dataset TEXT NOT NULL, market TEXT NOT NULL, trade_date TEXT NOT NULL,
          record_key TEXT NOT NULL, symbol TEXT, payload TEXT NOT NULL,
          collected_at TEXT NOT NULL,
          PRIMARY KEY(dataset,market,trade_date,record_key));
        CREATE TABLE IF NOT EXISTS tasks(
          market TEXT NOT NULL, trade_date TEXT NOT NULL, scope TEXT NOT NULL,
          task_key TEXT NOT NULL, status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0, message TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(market,trade_date,scope,task_key));
        CREATE TABLE IF NOT EXISTS runs(
          id INTEGER PRIMARY KEY, market TEXT NOT NULL, trade_date TEXT NOT NULL,
          scope TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
          finished_at TEXT, message TEXT);
        CREATE TABLE IF NOT EXISTS schedule_slots(
          slot TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS universe_snapshots(
          market TEXT,trade_date TEXT,scope TEXT,kind TEXT,expected_ids TEXT NOT NULL,
          catalog_count INTEGER NOT NULL,updated_at TEXT NOT NULL,
          PRIMARY KEY(market,trade_date,scope,kind));
        CREATE TABLE IF NOT EXISTS operational_alerts(
          alert_key TEXT PRIMARY KEY,severity TEXT NOT NULL,message TEXT NOT NULL,
          first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,resolved_at TEXT);
        CREATE TABLE IF NOT EXISTS worker_heartbeat(
          id INTEGER PRIMARY KEY CHECK(id=1),updated_at TEXT NOT NULL,state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS series_rebuilds(
          id INTEGER PRIMARY KEY,instrument_id TEXT NOT NULL,created_at TEXT NOT NULL,
          old_rows TEXT NOT NULL,report TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS instrument_lifecycle(
          instrument_id TEXT PRIMARY KEY REFERENCES instruments(id),status TEXT NOT NULL,
          listed_date TEXT,delisted_date TEXT,catalog_date TEXT,source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS catalog_publications(
          market TEXT,kind TEXT,classification TEXT,observed_date TEXT,expected_ids TEXT NOT NULL,
          source TEXT NOT NULL,updated_at TEXT NOT NULL,
          PRIMARY KEY(market,kind,classification));
        CREATE TABLE IF NOT EXISTS lifecycle_observations(
          instrument_id TEXT,source TEXT,observed_date TEXT,payload TEXT NOT NULL,verified_at TEXT NOT NULL,
          PRIMARY KEY(instrument_id,source,observed_date));
        CREATE TABLE IF NOT EXISTS query_snapshots(
          cache_key TEXT PRIMARY KEY,payload TEXT NOT NULL,expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bar_revisions(
          id INTEGER PRIMARY KEY,instrument_id TEXT,trade_date TEXT,previous_payload TEXT NOT NULL,
          new_payload TEXT NOT NULL,recorded_at TEXT NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history_requests(
          instrument_id TEXT PRIMARY KEY REFERENCES instruments(id),start_date TEXT NOT NULL,
          target_date TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feed_snapshots(
          feed_key TEXT PRIMARY KEY,payload TEXT NOT NULL,verified_at TEXT NOT NULL,status TEXT NOT NULL,
          message TEXT);
        CREATE TABLE IF NOT EXISTS feed_attempts(
          feed_key TEXT PRIMARY KEY,last_attempt TEXT NOT NULL,last_success TEXT,
          attempts INTEGER NOT NULL,status TEXT NOT NULL,message TEXT);
        ''')
        for table, columns in {
            "tasks": {"reason_code":"TEXT", "retryable":"INTEGER NOT NULL DEFAULT 1"},
            "schedule_slots": {"attempts":"INTEGER NOT NULL DEFAULT 1", "next_retry_at":"TEXT"},
        }.items():
            existing={r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES(2)")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES(3)")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES(4)")


def instrument(item):
    validate_instrument(item)
    with connect() as con:
        _instrument(con, item)


def validate_instrument(item):
    if item["kind"] not in ("stock","etf","industry","concept"):
        raise ValueError("invalid instrument kind")
    if item["id"] != f"{item['market']}.{item['kind']}.{item['symbol']}":
        raise ValueError("instrument ID does not match symbol")
    if item["market"] not in ("CN", "US") or item["currency"] != ("CNY" if item["market"] == "CN" else "USD"):
        raise ValueError("invalid instrument market/currency")
    if item["market"] == "CN" and item["kind"] in ("stock", "etf") and not re.fullmatch(r"\d{6}", item["symbol"]):
        raise ValueError("invalid CN symbol")
    if item["market"] == "CN" and item["kind"] in ("stock", "etf") and item["source_code"] not in (
            item["symbol"],"sh"+item["symbol"],"sz"+item["symbol"],"bj"+item["symbol"]):
        raise ValueError("source symbol differs from instrument")
    if item["market"] == "US" and item["source_code"] not in (item["symbol"],"105."+item["symbol"],"106."+item["symbol"],"107."+item["symbol"]):
        raise ValueError("source symbol differs from instrument")


def _instrument(con, item):
    con.execute('''INSERT INTO instruments VALUES(?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET name=excluded.name, source_code=excluded.source_code,
          currency=excluded.currency,group_name=excluded.group_name, updated_at=excluded.updated_at''',
          (item["id"], item["market"], item["kind"], item["symbol"], item["name"],
           item["source_code"], item["currency"], item.get("group_name"), now_iso()))


def publish_catalog(items, market, kind, day, source, classification="", full=False):
    """Publish validated metadata independently of history; absence is not proof of delisting."""
    if not items:
        raise ValueError("empty catalog cannot replace a publication")
    from datetime import date
    observed = date.fromisoformat(day)
    from .calendars import CN, US
    if observed > datetime.now(CN if market == "CN" else US).date():
        raise ValueError("future catalog observation")
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate catalog ID")
    for item in items:
        validate_instrument(item)
        if item["market"] != market or item["kind"] != kind:
            raise ValueError("catalog scope mismatch")
        if item.get("listing_status", "listed") not in ("listed", "delisted", "catalog_missing"):
            raise ValueError("invalid listing status")
        if item.get("listed_date"):
            if date.fromisoformat(item["listed_date"]) > observed:
                raise ValueError("future listing date")
        if item.get("delisted_date"):
            date.fromisoformat(item["delisted_date"])
            if item["delisted_date"] > day or (item.get("listed_date") and item["listed_date"] > item["delisted_date"]):
                raise ValueError("invalid delisting period")
        if item.get("listing_status") == "delisted" and not item.get("delisted_date"):
            raise ValueError("explicit delisting date required")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        old_publication = con.execute("SELECT observed_date FROM catalog_publications WHERE market=? AND kind=? AND classification=?",
                                      (market,kind,classification)).fetchone()
        if old_publication and day < old_publication[0]:
            raise ValueError("catalog observation moved backwards")
        for item in items:
            previous = con.execute("SELECT * FROM instrument_lifecycle WHERE instrument_id=?", (item["id"],)).fetchone()
            if previous and previous["catalog_date"] and day < previous["catalog_date"]:
                raise ValueError("lifecycle observation moved backwards")
            _instrument(con, item)
            if "listing_status" in item:
                con.execute("INSERT OR REPLACE INTO lifecycle_observations VALUES(?,?,?,?,?)",
                            (item["id"],source,day,json.dumps(item,ensure_ascii=False,allow_nan=False),now_iso()))
            # Generic catalogs cannot erase exchange-confirmed dates or undo delisting.
            # Explicit relisting requires a new listing date after the old delisting.
            listed, delisted = item.get("listed_date"), item.get("delisted_date")
            status = item.get("listing_status", "listed")
            lifecycle_source = source
            if previous and previous["status"] == "delisted" and status != "delisted":
                if not (listed and previous["delisted_date"] and listed > previous["delisted_date"]):
                    status, listed, delisted, lifecycle_source = (previous[k] for k in ("status","listed_date","delisted_date","source"))
            elif previous and not listed and not delisted:
                listed, delisted, lifecycle_source = (previous[k] for k in ("listed_date","delisted_date","source"))
            con.execute('''INSERT INTO instrument_lifecycle VALUES(?,?,?,?,?,?)
              ON CONFLICT(instrument_id) DO UPDATE SET status=excluded.status,
              listed_date=COALESCE(excluded.listed_date,instrument_lifecycle.listed_date),
              delisted_date=excluded.delisted_date,catalog_date=excluded.catalog_date,source=excluded.source''',
              (item["id"], status, listed, delisted, day, lifecycle_source))
        if full:
            old = con.execute("SELECT expected_ids FROM catalog_publications WHERE market=? AND kind=? AND classification=?",
                              (market, kind, classification)).fetchone()
            previous_ids = set(json.loads(old[0]) if old else [])
            if kind in ("stock", "etf"):
                previous_ids.update(r[0] for r in con.execute("SELECT id FROM instruments WHERE market=? AND kind=?", (market,kind)))
            for identifier in previous_ids - set(ids):
                con.execute("UPDATE instrument_lifecycle SET status='catalog_missing',catalog_date=? WHERE instrument_id=? AND status!='delisted'", (day, identifier))
            con.execute("INSERT OR REPLACE INTO catalog_publications VALUES(?,?,?,?,?,?,?)",
                        (market, kind, classification, day, json.dumps(sorted(ids)), source, now_iso()))


def validate_bars(identifier, rows):
    if not rows:
        return set()
    days = [r["trade_date"] for r in rows]
    if len(days) != len(set(days)):
        raise ValueError("duplicate daily bar")
    from .calendars import session_close, latest_ready
    from .settings import config
    from datetime import date
    market = identifier.split(".")[0]
    cfg = config()
    cutoff = latest_ready(market, cfg).isoformat()
    for r in rows:
        if r.get("adjustment","none") != "none":
            raise ValueError("unsupported bar adjustment")
        if r["volume_unit"] not in ("share","lot_100_shares","source_unit_unverified"):
            raise ValueError("unknown volume unit")
        if r["trade_date"] > cutoff:
            raise ValueError("future daily bar")
        if session_close(market, date.fromisoformat(r["trade_date"]), cfg) is None:
            raise ValueError("daily bar is not a market session")
        prices = [r[k] for k in ("open", "high", "low", "close")]
        if any(v is None or not math.isfinite(v) or v <= 0 for v in prices):
            raise ValueError("invalid OHLC")
        if r["low"] > min(r["open"], r["close"]) or r["high"] < max(r["open"], r["close"]):
            raise ValueError("inconsistent OHLC bounds")
        for k in ("volume", "amount"):
            if r.get(k) is not None and (not math.isfinite(r[k]) or r[k] < 0):
                raise ValueError("invalid " + k)
        if r.get("change_pct") is not None and not math.isfinite(r["change_pct"]):
            raise ValueError("invalid change_pct")
    profiles = {(r.get("source", "akshare/eastmoney"), r["volume_unit"]) for r in rows}
    if len(profiles) != 1 or any(not s or not u for s, u in profiles):
        raise ValueError("mixed source/volume units")
    return profiles


def put_bars(identifier, rows):
    if not rows:
        return
    profiles = validate_bars(identifier, rows)
    with connect() as con:
        existing = {(r[0], r[1]) for r in con.execute("SELECT DISTINCT source,volume_unit FROM bars WHERE instrument_id=? AND adjustment='none'", (identifier,))}
        if existing and existing != profiles:
            raise ValueError("source or volume unit switch requires a separate rebuild")
        fields = ("open", "high", "low", "close", "volume", "volume_unit", "amount", "change_pct", "source")
        for r in rows:
            old = con.execute("SELECT * FROM bars WHERE instrument_id=? AND trade_date=? AND adjustment='none'", (identifier, r["trade_date"])).fetchone()
            if old and any(old[k] != r.get(k, "akshare/eastmoney" if k == "source" else None) for k in fields):
                archived={k:({"invalid_numeric":str(v)} if isinstance(v,float) and not math.isfinite(v) else v)
                          for k,v in dict(old).items()}
                con.execute("INSERT INTO bar_revisions(instrument_id,trade_date,previous_payload,new_payload,recorded_at,reason) VALUES(?,?,?,?,?,?)",
                            (identifier, r["trade_date"], json.dumps(archived, allow_nan=False), json.dumps(r, allow_nan=False), now_iso(), "provider_revision; corporate_action_unverified"))
        con.executemany('''INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(instrument_id,trade_date,adjustment) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
          volume=excluded.volume, volume_unit=excluded.volume_unit, amount=excluded.amount,
          change_pct=excluded.change_pct, source=excluded.source, collected_at=excluded.collected_at''',
          [(identifier, r["trade_date"], "none", r["open"], r["high"], r["low"],
            r["close"], r["volume"], r["volume_unit"], r["amount"], r["change_pct"],
            r.get("source", "akshare/eastmoney"), now_iso()) for r in rows])


def put_dataset(dataset, market, day, rows):
    # Replace only after a nonempty, fully validated response. Empty is not proof of no records.
    if not rows:
        raise ValueError("empty dataset must not overwrite prior records")
    with connect() as con:
        con.execute("DELETE FROM datasets WHERE dataset=? AND market=? AND trade_date=?",
                    (dataset, market, day))
        con.executemany("INSERT INTO datasets VALUES(?,?,?,?,?,?,?)",
                        [(dataset, market, day, r["key"], r.get("symbol"),
                          json.dumps(r, ensure_ascii=False, allow_nan=False), now_iso()) for r in rows])


def feed_attempt(key, status, message=None):
    now=now_iso()
    with connect() as con:
        con.execute('''INSERT INTO feed_attempts VALUES(?,?,?,1,?,?) ON CONFLICT(feed_key)
          DO UPDATE SET last_attempt=excluded.last_attempt,
          last_success=COALESCE(excluded.last_success,feed_attempts.last_success),
          attempts=feed_attempts.attempts+1,status=excluded.status,message=excluded.message''',
          (key,now,now if status=="complete" else None,status,message))


def backup(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve() == (data_dir() / "market.sqlite3").resolve():
        raise ValueError("backup destination cannot be the live database")
    with connect() as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
