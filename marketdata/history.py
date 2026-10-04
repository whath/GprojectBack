"""Trading-session windows and transparent history coverage."""
from datetime import date, timedelta

from . import db
from .calendars import session_close


def sessions(market, end, count, cfg):
    days = []
    cursor = end
    # A generous bounded search also handles long closures and calendar overrides.
    for _ in range(count * 4 + 60):
        if session_close(market, cursor, cfg) is not None:
            days.append(cursor.isoformat())
        if len(days) == count:
            return list(reversed(days))
        cursor -= timedelta(days=1)
    raise ValueError("calendar cannot supply the requested window")


def audit(identifier, end, cfg, count=250):
    expected = sessions(identifier.split(".")[0], date.fromisoformat(end), count, cfg)
    with db.connect() as con:
        rows = con.execute("SELECT trade_date FROM bars WHERE instrument_id=? AND trade_date<=? AND adjustment='none' ORDER BY trade_date", (identifier, end)).fetchall()
        lifecycle = con.execute("SELECT listed_date FROM instrument_lifecycle WHERE instrument_id=?", (identifier,)).fetchone()
        suspended = {r[0] for r in con.execute("SELECT trade_date FROM datasets WHERE dataset='suspensions' AND market=? AND symbol=?", (identifier.split(".")[0], identifier.rsplit(".", 1)[-1]))}
    dates = {r[0] for r in rows}
    listed = lifecycle[0] if lifecycle else None
    if listed:
        expected = [d for d in expected if d >= listed]
    missing = sorted(set(expected) - dates - suspended)
    return {"instrument_id": identifier, "trade_date": end, "window": count,
            "bar_count": len(rows), "first_date": rows[0][0] if rows else None,
            "listed_date": listed, "missing_count": len(missing), "missing_dates": missing,
            "suspended_dates": sorted(set(expected) & suspended), "complete": not missing,
            "basis": "market sessions; independently recorded full-session suspensions; known listing date only"}


def run_slice(cfg, now=None):
    """Process explicit history expansion requests for both markets, independently of quotes."""
    from datetime import datetime, timezone
    from .calendars import CN, latest_ready, CN_COLLECTION_TIMES
    from .pipeline import Collector, CollectorLock
    import time
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(CN)
    if any(timedelta(0) <= datetime.combine(local.date(), t, CN) - local < timedelta(minutes=5) for t in CN_COLLECTION_TIMES):
        return
    with db.connect() as con:
        requests = [dict(r) for r in con.execute('''SELECT q.*,i.* FROM history_requests q
          JOIN instruments i ON i.id=q.instrument_id ORDER BY q.updated_at,q.instrument_id''')]
    began = time.monotonic()
    attempted = 0
    with CollectorLock():
        for item in requests:
            if time.monotonic() - began >= 60:
                break
            market = item["market"]
            if market == "CN" and (local.hour, local.minute) < (15, 10):
                continue
            day = min(date.fromisoformat(item["target_date"]), latest_ready(market, cfg))
            key = "bar:" + item["id"]
            with db.connect() as con:
                previous = con.execute("SELECT * FROM tasks WHERE market=? AND trade_date=? AND scope='requested_history' AND task_key=?", (market, day.isoformat(), key)).fetchone()
            if previous and (previous["attempts"] >= cfg.get("history_backfill_attempts",3) or
                    (now-datetime.fromisoformat(previous["updated_at"])).total_seconds()<3600):
                continue
            if market == "US":
                endpoint = "stock_us_hist"
            elif item["kind"] == "stock":
                endpoint = "stock_zh_a_daily" if item["source_code"].startswith("bj") else "stock_zh_a_hist_tx"
                if cfg.get("cn_sources", {}).get("stock") == "eastmoney":
                    endpoint = "stock_zh_a_hist"
            elif item["kind"] == "etf":
                endpoint = "fund_etf_hist_sina" if cfg.get("cn_sources", {}).get("etf") == "sina" else "fund_etf_hist_em"
            else:
                suffix = "index_ths" if item["symbol"].startswith("THS_") else "hist_em"
                endpoint = f"stock_board_{item['kind']}_{suffix}"
            collector = Collector(market, day, settings=dict(cfg, collection_budget_seconds=60,
                                  request_timeout_seconds=20, request_attempts=1), refresh=True)
            collector.scope = "requested_history"
            def extend(item=item, collector=collector, endpoint=endpoint):
                rows = collector.history(item, endpoint)
                cursor = date.fromisoformat(item["start_date"])
                expected = []
                with db.connect() as con:
                    lifecycle = con.execute("SELECT listed_date FROM instrument_lifecycle WHERE instrument_id=?", (item["id"],)).fetchone()
                    if lifecycle and lifecycle[0]:
                        cursor = max(cursor, date.fromisoformat(lifecycle[0]))
                    while cursor <= day:
                        if session_close(market, cursor, cfg) is not None:
                            expected.append(cursor.isoformat())
                        cursor += timedelta(days=1)
                    actual = {r[0] for r in con.execute("SELECT trade_date FROM bars WHERE instrument_id=? AND adjustment='none'", (item["id"],))}
                    suspensions = {r[0] for r in con.execute("SELECT trade_date FROM datasets WHERE dataset='suspensions' AND market=? AND symbol=?", (market,item["symbol"]))}
                    missing = set(expected)-actual-suspensions
                    if not missing:
                        con.execute("DELETE FROM history_requests WHERE instrument_id=? AND start_date=? AND target_date=?", (item["id"],item["start_date"],item["target_date"]))
                if missing:
                    from .normalize import PendingData
                    raise PendingData(f"historical sessions unverified: {len(missing)}; IPO/suspension requires evidence")
                return rows
            collector.task(key, extend, always=True)
            attempted += 1
    return {"attempted": attempted, "scope": "requested_history"}
