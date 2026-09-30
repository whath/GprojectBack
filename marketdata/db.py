import json
import sqlite3
from contextlib import contextmanager,closing
from datetime import datetime, timezone

from .settings import data_dir


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(data_dir() / "market.sqlite3", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
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


def instrument(item):
    with connect() as con:
        con.execute('''INSERT INTO instruments VALUES(?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET name=excluded.name, source_code=excluded.source_code,
          group_name=excluded.group_name, updated_at=excluded.updated_at''',
          (item["id"], item["market"], item["kind"], item["symbol"], item["name"],
           item["source_code"], item["currency"], item.get("group_name"), now_iso()))


def put_bars(identifier, rows):
    with connect() as con:
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


def backup(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.resolve() == (data_dir() / "market.sqlite3").resolve():
        raise ValueError("backup destination cannot be the live database")
    with connect() as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
