import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

from . import db, normalize
from .calendars import CN, validate_collection
from .provider import AKProvider, ProviderError
from .settings import config, data_dir

log = logging.getLogger(__name__)


class CollectorLock:
    """OS advisory lock released automatically on termination; cross-platform."""
    def __init__(self, filename="collector.lock"):
        self.filename=filename

    def __enter__(self):
        self.handle = (data_dir() / self.filename).open("a+b")
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0, 2)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.handle.close()
                raise RuntimeError("another collector is running") from None
        else:
            import fcntl
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.handle.close()
                raise RuntimeError("another collector is running") from None
        return self

    def __exit__(self, *args):
        self.handle.close()


class Collector:
    def __init__(self, market, day, sample=False, provider=None, refresh=False, settings=None, retry_only=False, progress=None):
        self.market, self.day, self.sample = market, day, sample
        self.cfg = settings or config()
        self.provider = provider or AKProvider(self.cfg)
        self.scope = "sample" if sample else "configured"
        self.refresh = refresh
        self.retry_only=retry_only
        self.progress=progress
        self.started=time.monotonic()

    def task(self, key, action, always=False):
        args = (self.market, self.day.isoformat(), self.scope, key)
        with db.connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE market=? AND trade_date=? AND scope=? AND task_key=?", args).fetchone()
            if row and self.retry_only and not key.startswith(("catalog:","quote_catalog:")):
                from .operations import reason
                if row["status"]=="complete" or not reason(row["status"],row["message"])[1]:
                    return None
            if row and row["status"] == "complete" and not self.refresh and not always:
                return None
            con.execute('''INSERT INTO tasks(market,trade_date,scope,task_key,status,attempts,updated_at)
              VALUES(?,?,?,?,'running',1,?) ON CONFLICT(market,trade_date,scope,task_key)
              DO UPDATE SET status='running',attempts=attempts+1,updated_at=excluded.updated_at''', (*args, db.now_iso()))
        value, count, message, status = None, 0, None, "complete"
        try:
            if time.monotonic()-self.started >= self.cfg.get("collection_budget_seconds",900):
                raise normalize.PendingData("collection budget deferred; resume on bounded retry")
            value = action()
            count = len(value) if isinstance(value, (list, dict)) else int(value or 0)
        except normalize.PendingData as exc:
            status, message = "pending", str(exc)
        except Exception as exc:
            status, message = "failed", f"{type(exc).__name__}: {str(exc)[:240]}"
        from .operations import reason
        reason_code,retryable=reason(status,message)
        with db.connect() as con:
            con.execute('''UPDATE tasks SET status=?,row_count=?,message=?,updated_at=?,reason_code=?,retryable=?
              WHERE market=? AND trade_date=? AND scope=? AND task_key=?''',
              (status, count, message, db.now_iso(),reason_code,int(retryable), *args))
        log.info("%s %s %s rows=%s %s", self.market, key, status, count, message or "")
        return value

    def call(self, endpoint, **kwargs):
        # A long CN batch may cross midnight; stop requests outside the allowed window.
        validate_collection(self.market, self.day, self.cfg)
        if self.progress:
            self.progress(f"collecting:{self.market}:{self.day}:{endpoint}")
        return self.provider.call(endpoint, **kwargs)

    def universe(self,kind,identifiers,catalog_count):
        with db.connect() as con:
            con.execute("INSERT OR REPLACE INTO universe_snapshots VALUES(?,?,?,?,?,?,?)",
                (self.market,self.day.isoformat(),self.scope,kind,json.dumps(sorted(set(identifiers))),catalog_count,db.now_iso()))

    def catalog(self, endpoint):
        rows = self.call(endpoint)
        if not rows:
            raise normalize.PendingData("empty instrument catalog")
        return rows

    def missing(self, label):
        raise normalize.PendingData(f"configured instrument missing from catalog: {label}")

    def history(self, item, endpoint):
        with db.connect() as con:
            latest = con.execute("SELECT max(trade_date) FROM bars WHERE instrument_id=? AND trade_date<=?", (item["id"], self.day.isoformat())).fetchone()[0]
            sources = {r[0] for r in con.execute("SELECT DISTINCT source FROM bars WHERE instrument_id=?", (item["id"],))}
        start = self.day - timedelta(days=self.cfg["history_days"])
        if latest:
            start = max(start, date.fromisoformat(latest) - timedelta(days=10))
        kwargs = dict(symbol=item["source_code"], start_date=start.strftime("%Y%m%d"),
                      end_date=self.day.strftime("%Y%m%d"), adjust="",
                      period="日k" if item["kind"] == "industry" else "daily")
        source = "akshare/eastmoney"
        alternative = endpoint in ("stock_zh_a_hist_tx", "stock_zh_a_daily", "fund_etf_hist_sina") or endpoint.endswith("_index_ths")
        if alternative:
            source = "akshare/" + ("tencent" if endpoint == "stock_zh_a_hist_tx" else "sina" if endpoint in ("fund_etf_hist_sina", "stock_zh_a_daily") else "ths")
            if sources and sources != {source}:
                raise normalize.PendingData("source switch requires a separate rebuild; existing series retained")
            if endpoint == "fund_etf_hist_sina":
                kwargs = {"symbol": item["source_code"]}
            else:
                kwargs.pop("period")
                if endpoint.endswith("_index_ths"):
                    kwargs.pop("adjust")
                elif endpoint == "stock_zh_a_hist_tx":
                    kwargs["timeout"] = 15
            raw = self.call(endpoint, **kwargs)
        elif endpoint == "stock_us_hist" and sources == {"akshare/sina"}:
            # Keep a continuous source series after a successful fallback.
            raw = self.call("stock_us_daily", symbol=item["symbol"], adjust="")
            source = "akshare/sina"
        else:
            try:
                raw = self.call(endpoint, **kwargs)
            except ProviderError:
                if endpoint != "stock_us_hist" or self.cfg.get("us_history_fallback") != "sina":
                    raise
                if sources and sources != {"akshare/sina"}:
                    raise normalize.PendingData("source switch requires a separate rebuild; existing series retained")
                log.warning("%s Eastmoney unavailable; using AKShare Sina daily fallback",item["id"])
                raw = self.call("stock_us_daily", symbol=item["symbol"], adjust="")
                source = "akshare/sina"
        if sources and sources != {source}:
            raise normalize.PendingData("source switch requires a separate rebuild; existing series retained")
        db.instrument(item)
        raw = [r for r in raw if normalize.day_string(r["日期"]) >= start.isoformat()]
        rows = normalize.bars(raw, self.day.isoformat(), item["kind"], self.market)
        for row in rows:
            row["source"] = source
        if rows:
            db.put_bars(item["id"], rows)
        if not rows or rows[-1]["trade_date"] != self.day.isoformat():
            raise normalize.PendingData("target daily bar absent (late source, suspension or missing history)")
        return rows

    def item(self, kind, symbol, name, source_code=None, group=None):
        return dict(id=f"{self.market}.{kind}.{symbol}", market=self.market, kind=kind,
                    symbol=symbol, name=name, source_code=source_code or symbol,
                    currency="CNY" if self.market == "CN" else "USD", group_name=group)

    def cn(self):
        if self.cfg.get("cn_full_close_quotes") and not self.sample:
            from .quotes import collect
            collect(self)
        # Disclosures are small and time-sensitive; do not queue them behind full history initialization.
        self.collect_lhb()
        sources = self.cfg.get("cn_sources", {"stock":"eastmoney", "etf":"eastmoney", "boards":"eastmoney"})
        routes = {
            ("stock", "eastmoney"): ("stock_zh_a_spot_em", "stock_zh_a_hist"),
            ("stock", "tencent"): ("stock_info_a_code_name", "stock_zh_a_hist_tx"),
            ("etf", "eastmoney"): ("fund_etf_spot_em", "fund_etf_hist_em"),
            ("etf", "sina"): ("fund_etf_category_sina", "fund_etf_hist_sina"),
        }
        for kind, cfg_key in (("stock", "cn_stocks"), ("etf", "cn_etfs")):
            endpoint, history = routes[(kind, sources[kind])]
            def catalog(e=endpoint):
                if e == "fund_etf_category_sina":
                    rows = self.call(e, symbol="ETF基金")
                    if not rows:
                        raise normalize.PendingData("empty ETF catalog")
                    return rows
                return self.catalog(e)
            rows = self.task(f"catalog:{kind}", catalog, always=True)
            if rows is None:
                continue
            selected = self.cfg[cfg_key]
            all_items = self.cfg["cn_all_stocks" if kind == "stock" else "cn_all_etfs"] and not self.sample
            self.universe(kind,[f"CN.{kind}.{str(r['代码']).zfill(6)}" for r in rows] if all_items else [f"CN.{kind}.{s}" for s in selected],len(rows))
            if not all_items:
                found = {str(r["代码"]).zfill(6) for r in rows}
                for symbol in set(selected)-found:
                    self.task(f"bar:CN.{kind}.{symbol}",lambda s=symbol:self.missing(s),always=True)
            for row in rows:
                symbol = str(row["代码"]).zfill(6)
                if not all_items and symbol not in selected:
                    continue
                source_code = row.get("source_code", symbol)
                item_history = history
                if history == "stock_zh_a_hist_tx":
                    prefix = "sh" if symbol.startswith("6") else "sz" if symbol.startswith(("0", "3")) else "bj"
                    source_code = prefix + symbol
                    if prefix == "bj":
                        item_history = "stock_zh_a_daily"
                item = self.item(kind, symbol, str(row["名称"]), source_code)
                self.task(f"bar:{item['id']}", lambda i=item,e=item_history: self.history(i,e))
        for kind in ("industry", "concept"):
            board_source = sources["boards"]
            if board_source not in ("ths", "eastmoney"):
                raise ValueError("unsupported board classification source")
            suffix = "ths" if board_source == "ths" else "em"
            rows = self.task(f"catalog:{kind}", lambda k=kind: self.catalog(f"stock_board_{k}_name_{suffix}"), always=True)
            if rows is None:
                continue
            selected = self.cfg["cn_boards"][kind]
            full=not self.sample and self.cfg["cn_all_boards"]
            expected=[f"CN.{kind}.{r['板块代码']}" for r in rows if full or r["板块代码"] in selected or r["板块名称"] in selected]
            if not full:
                found={str(r[k]) for r in rows for k in ("板块代码","板块名称")}
                expected += [f"UNRESOLVED.{kind}.{s}" for s in set(selected)-found]
            self.universe(kind,expected,len(rows))
            if self.sample or not self.cfg["cn_all_boards"]:
                found = {str(r[key]) for r in rows for key in ("板块代码","板块名称")}
                for symbol in set(selected)-found:
                    self.task(f"missing_board:{kind}:{symbol}",lambda s=symbol:self.missing(s),always=True)
            for row in rows:
                code, name = str(row["板块代码"]), str(row["板块名称"])
                if (self.sample or not self.cfg["cn_all_boards"]) and name not in selected and code not in selected:
                    continue
                item = self.item(kind, code, name, name if suffix == "ths" else code, group=board_source)
                history = f"stock_board_{kind}_index_ths" if suffix == "ths" else f"stock_board_{kind}_hist_em"
                self.task(f"bar:{item['id']}", lambda i=item,e=history: self.history(i,e))
                # Current membership cannot reconstruct historical membership.
                if self.day == datetime.now(CN).date():
                    def membership(i=item,k=kind):
                        if suffix == "ths":
                            raise normalize.PendingData("THS membership unavailable: no verified complete provider; no partial snapshot published")
                        records = normalize.members(self.call(f"stock_board_{k}_cons_em", symbol=i["source_code"]))
                        db.put_dataset("members:" + i["id"], "CN", self.day.isoformat(), records)
                        return records
                    self.task(f"members:{item['id']}", membership)

    def collect_lhb(self):
        day = self.day.strftime("%Y%m%d")
        for institutions, endpoint, dataset in ((False,"stock_lhb_detail_em","lhb"), (True,"stock_lhb_jgmmtj_em","lhb_institutions")):
            def fetch(ins=institutions,e=endpoint,d=dataset):
                rows = normalize.lhb(self.call(e,start_date=day,end_date=day), self.day.isoformat(), ins)
                db.put_dataset(d,"CN",self.day.isoformat(),rows)
                return rows
            self.task(dataset, fetch, always=True)
        with db.connect() as con:
            symbols = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM datasets WHERE dataset='lhb' AND trade_date=?",(self.day.isoformat(),))]
        if self.sample:
            symbols = symbols[:2]
        for symbol in symbols:
            for direction in ("买入","卖出"):
                def fetch(s=symbol,d=direction):
                    rows = normalize.seats(self.call("stock_lhb_stock_detail_em",symbol=s,date=day,flag=d),s,d)
                    db.put_dataset(f"lhb_seats:{s}:{d}","CN",self.day.isoformat(),rows)
                    return rows
                self.task(f"seats:{symbol}:{direction}",fetch)

    def us(self):
        universe = [(s,"etf",n,"sector") for s,n in self.cfg["us_sectors"].items()]
        universe += [(s,"stock",s,g) for s,g in self.cfg["us_ai"].items()]
        universe += [(self.cfg["us_benchmark"],"etf",self.cfg["us_benchmark"],"benchmark")]
        if self.sample:
            ai_sample = next(iter(self.cfg["us_ai"]), None)
            universe = universe[:2] + [u for u in universe if u[0] in (ai_sample,self.cfg["us_benchmark"])]
        requested = sorted({item[0] for item in universe})
        for kind in ("stock","etf"):
            self.universe(kind,[f"US.{k}.{s}" for s,k,_,_ in universe if k==kind],len(requested))
        def resolve():
            # Reuse previously resolved instrument identifiers; only new tickers need discovery.
            with db.connect() as con:
                configured={r["symbol"]:r["source_code"] for r in con.execute("SELECT symbol,source_code FROM instruments WHERE market='US'")}
            configured.update(self.cfg.get("us_source_codes",{}))
            records = [{"代码":configured[s],"名称":s} for s in requested if s in configured]
            unresolved = [s for s in requested if s not in configured]
            for offset in range(0,len(unresolved),50):
                records.extend(self.call("us_watchlist_catalog",symbols=unresolved[offset:offset+50]))
            if not records:
                raise normalize.PendingData("empty US metadata response")
            return records
        rows = self.task("catalog:us", resolve, always=True)
        mapping = dict(self.cfg.get("us_source_codes",{}))
        if rows is None:
            with db.connect() as con:
                for cached in con.execute("SELECT symbol,source_code FROM instruments WHERE market='US'"):
                    mapping.setdefault(cached["symbol"],cached["source_code"])
        for row in rows or []:
            code = str(row["代码"])
            symbol = code.split(".",1)[-1]
            # Ambiguous tickers require explicit configuration, never silently select one.
            if symbol in mapping and mapping[symbol] != code and symbol not in self.cfg.get("us_source_codes",{}):
                mapping[symbol] = None
            elif symbol not in mapping:
                mapping[symbol] = code
        for symbol, kind, name, group in universe:
            def fetch(s=symbol,k=kind,n=name,g=group):
                code = mapping.get(s)
                if not code:
                    raise normalize.PendingData("source code not resolved; ETF/stock coverage unverified")
                return self.history(self.item(k,s,n,code,g),"stock_us_hist")
            self.task(f"bar:US.{kind}.{symbol}",fetch)

    def run(self):
        validate_collection(self.market,self.day,self.cfg)
        db.init()
        with CollectorLock():
            with db.connect() as con:
                con.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (db.now_iso(),))
                con.execute("UPDATE tasks SET status='interrupted' WHERE status='running'")
                run_id = con.execute("INSERT INTO runs(market,trade_date,scope,status,started_at) VALUES(?,?,?,'running',?)",
                                     (self.market,self.day.isoformat(),self.scope,db.now_iso())).lastrowid
            try:
                self.cn() if self.market == "CN" else self.us()
                with db.connect() as con:
                    counts = dict(con.execute("SELECT status,count(*) FROM tasks WHERE market=? AND trade_date=? AND scope=? GROUP BY status",
                                               (self.market,self.day.isoformat(),self.scope)).fetchall())
                status = "complete" if counts and set(counts) == {"complete"} else "partial"
                message = json.dumps(counts)
            except Exception as exc:
                status,message = "failed",f"{type(exc).__name__}: {str(exc)[:240]}"
            with db.connect() as con:
                con.execute("UPDATE runs SET status=?,message=?,finished_at=? WHERE id=?",(status,message,db.now_iso(),run_id))
            return dict(run_id=run_id,market=self.market,trade_date=self.day.isoformat(),scope=self.scope,status=status,details=message)
