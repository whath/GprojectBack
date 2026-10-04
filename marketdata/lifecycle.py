"""Exchange listing dates and explicit delisting reports through AKShare."""
from datetime import datetime, timedelta, timezone

from . import db, normalize
from .pipeline import Collector, CollectorLock

ROUTES = (
    ("stock_info_sh_name_code", {"symbol": "主板A股"}, "证券代码", "证券简称", None),
    ("stock_info_sh_name_code", {"symbol": "科创板"}, "证券代码", "证券简称", None),
    ("stock_info_sz_name_code", {"symbol": "A股列表"}, "A股代码", "A股简称", None),
    ("stock_info_bj_name_code", {}, "证券代码", "证券简称", None),
    ("stock_info_sh_delist", {"symbol": "全部"}, "公司代码", "公司简称", "暂停上市日期"),
    ("stock_info_sz_delist", {"symbol": "终止上市公司"}, "证券代码", "证券简称", "终止上市日期"),
)


def publish(rows, endpoint, code_column, name_column, delist_column, observed, catalog_key=None):
    items = {}
    for row in rows:
        symbol = str(row[code_column]).zfill(6)
        # SSE's report also contains B shares, which are outside this A-share contract.
        if endpoint.startswith("stock_info_sh") and not symbol.startswith("6"):
            continue
        listed = normalize.day_string(row["A股上市日期" if code_column == "A股代码" else "上市日期"])
        if listed > observed:
            raise ValueError("future listing date")
        prefix = "sh" if symbol.startswith("6") else "sz" if symbol.startswith(("0", "3")) else "bj"
        item = dict(id="CN.stock." + symbol, market="CN", kind="stock", symbol=symbol,
                    name=str(row[name_column]), source_code=prefix + symbol, currency="CNY",
                    listed_date=listed, listing_status="delisted" if delist_column else "listed")
        if delist_column:
            item["delisted_date"] = normalize.day_string(row[delist_column])
            if not listed <= item["delisted_date"] <= observed:
                raise ValueError("invalid delisting date")
        previous=items.get(item["id"])
        if previous:
            if not delist_column or previous["name"]!=item["name"] or previous["delisted_date"]!=item["delisted_date"]:
                raise ValueError("conflicting exchange listing identity")
            candidates=set(previous.get("reported_listing_dates",[previous["listed_date"]]))|{listed}
            previous["reported_listing_dates"]=sorted(candidates)
            previous["listed_date"]=next(iter(candidates)) if len(candidates)==1 else None
            previous["listing_date_ambiguous"]=len(candidates)>1
        else:
            items[item["id"]]=item
    if not items:
        raise normalize.PendingData("empty exchange lifecycle report is unverified")
    db.publish_catalog(list(items.values()), "CN", "stock", observed, "akshare/" + endpoint)
    if catalog_key and not delist_column:
        import json
        with db.connect() as con:
            con.execute("INSERT OR REPLACE INTO catalog_publications VALUES('CN','stock',?,?,?,?,?)",
                        ("exchange/"+catalog_key,observed,json.dumps(sorted(items)),"akshare/"+endpoint,db.now_iso()))
    return len(items)


def report():
    """Compare the exposed listed directory to all four independent exchange catalogs."""
    import json
    keys=[e+":"+str(k.get("symbol","")) for e,k,_,_,_ in ROUTES[:4]]
    with db.connect() as con:
        publications={r["classification"].removeprefix("exchange/"):dict(r) for r in con.execute(
            "SELECT * FROM catalog_publications WHERE market='CN' AND kind='stock' AND classification LIKE 'exchange/%'")}
        published={r[0] for r in con.execute("SELECT i.id FROM instruments i LEFT JOIN instrument_lifecycle l ON l.instrument_id=i.id WHERE i.market='CN' AND i.kind='stock' AND COALESCE(l.status,'listed')='listed'")}
    available={k:publications[k] for k in keys if k in publications}
    expected=set().union(*(set(json.loads(r["expected_ids"])) for r in available.values()))
    missing=sorted(expected-published)
    extras=sorted(published-expected)
    dates={r["observed_date"] for r in available.values()}
    known=set(available)==set(keys) and len(dates)==1
    return dict(market="CN",kind="stock",basis="Shanghai main A/STAR, Shenzhen A, Beijing catalogs through AKShare",
                exchange_catalogs_complete=known,directory_complete=known and not missing and not extras,
                expected=len(expected) if known else None,published_count=len(published),missing_ids=missing,
                unverified_ids=extras,missing_catalogs=sorted(set(keys)-set(available)),
                catalogs=[dict(key=k,observed_date=r["observed_date"],count=len(json.loads(r["expected_ids"])),
                               source=r["source"],verified_at=r["updated_at"]) for k,r in available.items()],
                note="complete proves agreement at the reported catalog observation date; not every historical bar")


def run_slice(cfg):
    import time
    from .calendars import CN, latest_ready, CN_COLLECTION_TIMES
    now = datetime.now(timezone.utc)
    local = now.astimezone(CN)
    if (local.hour, local.minute) < (15, 10) or any(
            timedelta(0) <= datetime.combine(local.date(), t, CN)-local < timedelta(minutes=5)
            for t in CN_COLLECTION_TIMES):
        return
    day = latest_ready("CN", cfg)
    collector = Collector("CN", day, settings=dict(cfg, request_timeout_seconds=20,request_attempts=1,collection_budget_seconds=60))
    collector.scope = "lifecycle"
    began = time.monotonic()
    with CollectorLock():
        for endpoint, kwargs, code, name, delist in ROUTES:
            key = endpoint + ":" + str(kwargs.get("symbol", ""))
            with db.connect() as con:
                state = con.execute("SELECT * FROM tasks WHERE market='CN' AND trade_date=? AND scope='lifecycle' AND task_key=?", (day.isoformat(),key)).fetchone()
            if state and (state["status"] == "complete" or state["attempts"] >= 3 or
                    (now-datetime.fromisoformat(state["updated_at"])).total_seconds()<3600):
                continue
            if time.monotonic()-began>=60:
                break
            collector.task(key, lambda e=endpoint,k=kwargs,c=code,n=name,d=delist,key=key:
                           publish(collector.call(e, **k),e,c,n,d,local.date().isoformat(),key))
