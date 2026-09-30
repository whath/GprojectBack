from datetime import date, timedelta

from . import db
from .calendars import session_close
from .settings import config


def metrics(identifier, day, cfg=None):
    cfg = cfg or config()
    with db.connect() as con:
        rows = [dict(r) for r in con.execute('''SELECT * FROM bars WHERE instrument_id=?
          AND trade_date<=? AND adjustment='none' ORDER BY trade_date DESC LIMIT 60''',(identifier,day))]
    if not rows:
        return {"status":"missing","trade_date":None}
    latest = rows[0]
    result = {"status":"current" if latest["trade_date"] == day else "stale",
              "source":latest["source"],
              "trade_date":latest["trade_date"],"close":latest["close"],
              "return_basis":"unadjusted_close_price; distributions/splits may distort returns",
              "change_1d_pct":None,"change_5d_pct":None,"change_20d_pct":None,
              "volume_ratio_20d":None,"breakout_20d":None,"breakdown_20d":None}
    sessions = []
    cursor = date.fromisoformat(latest["trade_date"])
    for _ in range(70):
        if session_close("US",cursor,cfg) is not None:
            sessions.append(cursor.isoformat())
        if len(sessions)==21:
            break
        cursor -= timedelta(days=1)
    by_day = {row["trade_date"]:row for row in rows}
    for n in (1,5,20):
        if len(sessions)>n and sessions[n] in by_day:
            result[f"change_{n}d_pct"] = (latest["close"]/by_day[sessions[n]]["close"]-1)*100
    previous = [by_day.get(d) for d in sessions[1:21]]
    if len(previous)==20 and all(previous):
        volumes = [r["volume"] for r in previous]
        if all(v is not None for v in volumes) and sum(volumes)>0 and latest["volume"] is not None:
            result["volume_ratio_20d"] = latest["volume"] / (sum(volumes)/20)
        result["breakout_20d"] = latest["close"]>max(r["high"] for r in previous)
        result["breakdown_20d"] = latest["close"]<min(r["low"] for r in previous)
    return result


def us_report(day, cfg, ai=False):
    universe = cfg["us_ai"] if ai else cfg["us_sectors"]
    benchmark = metrics("US.etf."+cfg["us_benchmark"],day,cfg)
    records = []
    for symbol,group in universe.items():
        item = metrics(f"US.{'stock' if ai else 'etf'}.{symbol}",day,cfg)
        item.update(symbol=symbol,group=group,relative_to_benchmark_1d_pp=None,alerts=[])
        current = item["status"]=="current"
        change = item.get("change_1d_pct")
        if current and change is not None:
            if benchmark["status"]=="current" and benchmark.get("change_1d_pct") is not None:
                item["relative_to_benchmark_1d_pp"] = change-benchmark["change_1d_pct"]
            if ai:
                thresholds=cfg["alerts"]
                if abs(change)>=thresholds["absolute_change_pct"]:
                    item["alerts"].append("large_price_move")
                if (item.get("volume_ratio_20d") or 0)>=thresholds["volume_ratio"]:
                    item["alerts"].append("high_volume")
                if item.get("breakout_20d"):
                    item["alerts"].append("close_above_previous_20_bar_high")
                if item.get("breakdown_20d"):
                    item["alerts"].append("close_below_previous_20_bar_low")
        records.append(item)
    eligible = sorted([r for r in records if r["status"]=="current" and r.get("change_1d_pct") is not None],
                      key=lambda r:r["change_1d_pct"],reverse=True)
    for rank,item in enumerate(eligible,1):
        item["rank"]=rank
    missing = [r for r in records if r not in eligible]
    gainers = [r for r in eligible if r["change_1d_pct"]>0]
    return {"trade_date":day,"scope":"configured_ai_watchlist" if ai else "SP500_sector_ETF_proxies",
            "expected_count":len(universe),"eligible_count":len(eligible),
            "complete":len(eligible)==len(universe),"benchmark":cfg["us_benchmark"],
            "top_label":"gainers" if gainers else "relative_leaders",
            "top":(gainers or eligible)[:5],"items":eligible+missing}
