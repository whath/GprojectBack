"""Dated close snapshots, separate from historical OHLC series and their coverage."""
import json
import re
from datetime import datetime

from . import db, normalize
from .calendars import CN


def parse_tencent(text, symbols):
    allowed = set(symbols)
    result = []
    seen = set()
    for code, body in re.findall(r'v_((?:sh|sz|bj)\d{6})="([^"]*)";', text):
        if code not in allowed or code in seen:
            raise ValueError("unexpected or duplicate quote symbol")
        seen.add(code)
        values = body.split("~")
        if len(values) < 38 or values[2] != code[2:]:
            continue
        try:
            stamp = datetime.strptime(values[30], "%Y%m%d%H%M%S").replace(tzinfo=CN)
            prices = {key: normalize.number(values[index]) for key,index in
                      (("open",5),("high",33),("low",34),("close",3),("previous_close",4))}
            if any(value is None or value <= 0 for value in prices.values()):
                continue
            if prices["low"] > min(prices["open"], prices["close"]) or prices["high"] < max(prices["open"], prices["close"]):
                continue
            result.append(dict(source_code=code, symbol=code[2:], source_time=stamp.isoformat(),
                               **prices, change_pct=normalize.number(values[32]),
                               volume=normalize.number(values[6]),volume_unit="source_unit_unverified",
                               amount=None,source="tencent/quote",adjustment="none"))
        except (ValueError, IndexError):
            continue
    return result


def fetch(symbols, day, kind):
    import akshare as ak
    from datetime import date, timedelta
    from .provider import adapt_frame
    from .settings import config
    if not 1 <= len(symbols) <= 5 or any(not re.fullmatch(r"(sh|sz|bj)\d{6}",s) for s in symbols):
        raise ValueError("invalid quote batch")
    target = date.fromisoformat(day)
    result = []
    for code in symbols:
        kwargs = dict(symbol=code, start_date=(target-timedelta(days=40)).strftime("%Y%m%d"),
                      end_date=target.strftime("%Y%m%d"), adjust="")
        endpoint = "fund_etf_hist_sina" if kind == "etf" else "stock_zh_a_daily" if code.startswith("bj") else "stock_zh_a_hist_tx"
        if kind == "etf":
            kwargs = {"symbol":code}
        elif endpoint == "stock_zh_a_hist_tx":
            kwargs["timeout"] = 15
        frame = adapt_frame(endpoint, getattr(ak, endpoint)(**kwargs), kwargs)
        raw = json.loads(frame.to_json(orient="records",date_format="iso"))
        raw = [r for r in raw if normalize.day_string(r["日期"]) <= day]
        rows = normalize.bars(raw, day, kind, "CN")
        by_day = {r["trade_date"]:r for r in rows}
        row = by_day.get(day)
        if row is None:
            continue
        from .history import sessions
        previous = by_day.get(sessions("CN",target,2,config())[0])
        result.append(dict(row, source_code=code,symbol=code[2:],source="akshare/"+endpoint,
                           source_time=datetime.combine(target,datetime.min.time().replace(hour=15),CN).isoformat(),
                           source_time_basis="market close for provider-dated daily bar; not a quote timestamp",
                           previous_close=previous["close"] if previous else None,adjustment="none"))
    return result


def collect(collector):
    """Each batch is a retry checkpoint; no thousands of per-symbol history requests."""
    import hashlib
    import time
    began = time.monotonic()
    if collector.day != datetime.now(CN).date():
        return  # A current quote cannot backfill yesterday.
    from .suspensions import saved
    def suspension_report():
        rows=collector.call("cn_suspensions",day=collector.day.isoformat())
        if rows:
            db.put_dataset("suspensions","CN",collector.day.isoformat(),rows)
        else:
            # Fetch validates the complete report before filtering full-session events.
            with db.connect() as con:
                con.execute("DELETE FROM datasets WHERE dataset='suspensions' AND trade_date=?",(collector.day.isoformat(),))
        return rows
    collector.task("suspensions",suspension_report,always=True)
    suspended=saved(collector.day.isoformat())
    catalogs = (("stock","stock_info_a_code_name",{}),
                ("etf","fund_etf_category_sina",{"symbol":"ETF基金"}))
    for kind, endpoint, kwargs in catalogs:
        rows = collector.task("quote_catalog:"+kind,lambda e=endpoint,k=kwargs:collector.call(e,**k),always=True)
        if not rows:
            continue
        catalog = {}
        for row in rows:
            symbol = str(row["代码"]).zfill(6)
            code = row.get("source_code") or (("sh" if symbol.startswith("6") else "sz" if symbol.startswith(("0","3")) else "bj")+symbol)
            if not re.fullmatch(r"(sh|sz|bj)\d{6}",code):
                raise ValueError("invalid catalog source code")
            catalog[code] = {"symbol":symbol,"name":str(row["名称"]),"kind":kind}
        codes = sorted(catalog)
        db.publish_catalog([dict(id=f"CN.{kind}.{r['symbol']}", market="CN",kind=kind,
                           symbol=r["symbol"],name=r["name"],source_code=code,currency="CNY")
                           for code,r in catalog.items()], "CN",kind,collector.day.isoformat(),
                           "akshare/catalog",full=True)
        with db.connect() as con:
            con.execute("INSERT OR REPLACE INTO universe_snapshots VALUES(?,?,?,?,?,?,?)",
                ("CN",collector.day.isoformat(),"quotes",kind,json.dumps(codes),len(codes),db.now_iso()))
        for offset in range(0,len(codes),5):
            if time.monotonic()-began >= collector.cfg.get("close_snapshot_budget_seconds",120):
                break
            batch=codes[offset:offset+5]
            batch_key=hashlib.sha256(",".join(batch).encode()).hexdigest()[:16]
            def save(batch=batch):
                records=collector.call("cn_close_quotes",symbols=batch,day=collector.day.isoformat(),kind=kind)
                valid={}
                for row in records:
                    stamp=datetime.fromisoformat(row["source_time"])
                    if stamp.date()!=collector.day or (stamp.hour,stamp.minute)<(15,0) or stamp>datetime.now(CN):
                        continue
                    valid[row["source_code"]]=dict(row,**catalog[row["source_code"]],trade_date=collector.day.isoformat())
                with db.connect() as con:
                    con.executemany("INSERT OR REPLACE INTO datasets VALUES(?,?,?,?,?,?,?)",
                        [("close_quotes:"+kind,"CN",collector.day.isoformat(),code,r["symbol"],
                          json.dumps(r,ensure_ascii=False,allow_nan=False),db.now_iso()) for code,r in valid.items()])
                unresolved=[code for code in batch if code not in valid and code[2:] not in suspended]
                if unresolved:
                    raise normalize.PendingData(f"target close quote absent or invalid: {len(unresolved)}/{len(batch)}")
                return len(valid)
            collector.task(f"quotes:{kind}:{batch_key}",save)


def report(day,kind,limit=100,offset=0):
    with db.connect() as con:
        manifest=con.execute("SELECT expected_ids FROM universe_snapshots WHERE market='CN' AND trade_date=? AND scope='quotes' AND kind=?",(day,kind)).fetchone()
        expected=set(json.loads(manifest[0])) if manifest else set()
        rows=con.execute("SELECT record_key,payload,collected_at FROM datasets WHERE dataset=? AND trade_date=? ORDER BY record_key",("close_quotes:"+kind,day)).fetchall()
    rows=[r for r in rows if r["record_key"] in expected]
    missing=sorted(expected-{r["record_key"] for r in rows})
    from .suspensions import saved
    suspended=saved(day)
    no_trade=[suspended[code[2:]] for code in missing if code[2:] in suspended]
    missing=[code for code in missing if code[2:] not in suspended]
    return {"trade_date":day,"kind":kind,"scope":"full_catalog_close_snapshots","historical_series_complete":None,
            "expected":len(expected),"total":len(rows),"complete":bool(expected) and not missing,
            "missing_count":len(missing),"missing_symbols":missing[:100],
            "suspended_count":len(no_trade),"suspended":no_trade,"complete_basis":"valid close quote or independently reported full-session suspension",
            "items":[dict(json.loads(r["payload"]),collected_at=r["collected_at"]) for r in rows[offset:offset+limit]]}
