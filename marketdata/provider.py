"""Bound each AKShare call by an OS-process timeout (including pagination)."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ALLOWED = {
    "cn_close_quotes",
    "cn_suspensions",
    "stock_info_a_code_name", "stock_zh_a_hist_tx", "stock_zh_a_daily", "fund_etf_category_sina", "fund_etf_hist_sina",
    "stock_board_industry_name_ths", "stock_board_concept_name_ths",
    "stock_board_industry_index_ths", "stock_board_concept_index_ths",
    "us_watchlist_catalog",
    "stock_zh_a_spot_em", "fund_etf_spot_em", "stock_us_spot_em",
    "stock_zh_a_hist", "fund_etf_hist_em", "stock_us_hist", "stock_us_daily",
    "stock_board_industry_name_em", "stock_board_concept_name_em",
    "stock_board_industry_hist_em", "stock_board_concept_hist_em",
    "stock_board_industry_cons_em", "stock_board_concept_cons_em",
    "stock_lhb_detail_em", "stock_lhb_jgmmtj_em", "stock_lhb_stock_detail_em",
}


class ProviderError(RuntimeError):
    pass


def adapt_frame(endpoint, frame, kwargs):
    """Map verified alternate feeds to the same wire columns, preserving units."""
    frame = frame.copy()
    if endpoint == "stock_info_a_code_name":
        return frame.rename(columns={"code": "代码", "name": "名称"})
    if endpoint == "fund_etf_category_sina":
        frame["source_code"] = frame["代码"]
        frame["代码"] = frame["代码"].str.replace(r"^(sh|sz)", "", regex=True)
        return frame
    if endpoint.endswith("_name_ths"):
        frame = frame.rename(columns={"name": "板块名称", "code": "板块代码"})
        frame["板块代码"] = "THS_" + frame["板块代码"].astype(str)
        return frame
    if endpoint in ("stock_us_daily", "stock_zh_a_hist_tx", "stock_zh_a_daily", "fund_etf_hist_sina"):
        frame = frame.rename(columns={"date":"日期", "open":"开盘", "high":"最高",
            "low":"最低", "close":"收盘", "volume":"成交量", "amount":"成交额"})
    if endpoint == "stock_zh_a_hist_tx":
        # AKShare 1.19.1 excludes sz000 equity codes from its lots-to-shares
        # conversion along with index codes. sh688 already returns shares.
        # This endpoint is called ONLY for
        # stocks from the A-share directory, never for index symbols.
        if kwargs["symbol"].startswith("sz000"):
            frame["成交量"] = frame["成交量"] * 100
        frame["volume_unit"] = "share"
    elif endpoint == "stock_zh_a_daily":
        frame["volume_unit"] = "share"
    elif endpoint == "fund_etf_hist_sina" or endpoint.endswith("_index_ths"):
        frame["volume_unit"] = "source_unit_unverified"
    if endpoint.endswith("_index_ths"):
        frame = frame.rename(columns={"开盘价":"开盘", "最高价":"最高", "最低价":"最低", "收盘价":"收盘"})
    return frame


def check_lhb_publication(endpoint, kwargs):
    """Recognize the source's explicit empty report, not arbitrary AKShare errors."""
    reports = {"stock_lhb_detail_em": "RPT_DAILYBILLBOARD_DETAILSNEW",
               "stock_lhb_jgmmtj_em": "RPT_ORGANIZATION_TRADE_DETAILS"}
    if endpoint not in reports:
        return
    import requests
    from datetime import datetime
    from .normalize import PendingData
    start, end = (datetime.strptime(kwargs[k], "%Y%m%d").date().isoformat() for k in ("start_date", "end_date"))
    response = requests.get("https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": reports[endpoint], "columns": "ALL", "pageSize": "1", "pageNumber": "1",
                "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") == 9201 and payload.get("success") is False and payload.get("result") is None:
        raise PendingData("source report empty (9201): not yet published or no-record state unverified")
    if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise ValueError("unexpected LHB publication response")


class AKProvider:
    def __init__(self, config):
        self.config = config

    def call(self, endpoint, **kwargs):
        if endpoint not in ALLOWED:
            raise ValueError("endpoint is not allowed")
        last_error = "unknown provider error"
        for attempt in range(self.config["request_attempts"]):
            with tempfile.TemporaryDirectory(prefix="market-call-") as folder:
                request = Path(folder) / "request.json"
                response = Path(folder) / "response.json"
                request.write_text(json.dumps({"endpoint": endpoint, "kwargs": kwargs}), encoding="utf-8")
                try:
                    environment = os.environ.copy()
                    if not self.config.get("use_system_proxy", True):
                        # Child-only bypass of OS-discovered proxies, never changes system settings.
                        environment["NO_PROXY"] = "*"
                        environment["no_proxy"] = "*"
                        environment["MARKET_DIRECT_REQUESTS"] = "1"
                    process = subprocess.run(
                        [sys.executable, "-m", "marketdata.provider", str(request), str(response)],
                        timeout=self.config["request_timeout_seconds"], capture_output=True, env=environment,
                    )
                    if process.returncode != 0 or not response.exists():
                        last_error = "provider subprocess failed"
                    else:
                        result = json.loads(response.read_text(encoding="utf-8"))
                        if "pending" in result:
                            from .normalize import PendingData
                            raise PendingData(result["pending"])
                        if "error" in result:
                            last_error = result["error"]
                        else:
                            time.sleep(self.config["request_spacing_seconds"])
                            return result["rows"]
                except subprocess.TimeoutExpired:
                    last_error = "provider request timed out"
            if attempt + 1 < self.config["request_attempts"]:
                time.sleep(2 ** (attempt + 1))
        raise ProviderError(f"{endpoint}: {last_error}")


def child(request, response):
    if os.getenv("MARKET_DIRECT_REQUESTS") == "1":
        import requests
        original_init = requests.sessions.Session.__init__
        def direct_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.trust_env = False
        # Only this disposable subprocess is affected; AKShare otherwise discovers OS proxies.
        requests.sessions.Session.__init__ = direct_init
    import akshare as ak
    args = json.loads(Path(request).read_text(encoding="utf-8"))
    try:
        if args["endpoint"] not in ALLOWED:
            raise ValueError("endpoint is not allowed")
        if args["endpoint"] == "cn_suspensions":
            from .suspensions import fetch
            rows = fetch(**args["kwargs"])
        elif args["endpoint"] == "cn_close_quotes":
            from .quotes import fetch
            rows = fetch(**args["kwargs"])
        elif args["endpoint"] == "us_watchlist_catalog":
            # Metadata only: query the same Eastmoney source for configured symbols.
            # Avoid downloading >10,000 live quotes merely to resolve a few codes.
            import requests
            symbols = args["kwargs"]["symbols"]
            if len(symbols)>100 or any(not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in s) for s in symbols):
                raise ValueError("invalid or oversized US symbol batch")
            http_response = requests.get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                params={"secids": ",".join(f"{m}.{s}" for s in symbols for m in (105,106,107)),
                        "fields":"f12,f13,f14","fltt":2}, timeout=20)
            http_response.raise_for_status()
            payload = http_response.json()
            if payload.get("rc") != 0:
                raise ValueError("source returned unsuccessful metadata response")
            entries = (payload.get("data") or {}).get("diff", [])
            if isinstance(entries, dict):
                entries = list(entries.values())
            rows = [{"代码":str(r["f13"])+"."+r["f12"],"名称":r["f14"]}
                    for r in entries if r["f12"] in symbols and r["f13"] in (105,106,107)]
        else:
            check_lhb_publication(args["endpoint"], args["kwargs"])
            frame = getattr(ak, args["endpoint"])(**args["kwargs"])
            frame = adapt_frame(args["endpoint"], frame, args["kwargs"])
            # pandas safely converts NaN/NaT/numpy values; no pickle across processes.
            rows = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
            if args["endpoint"] == "fund_etf_hist_sina":
                from .sina_recent import supplement
                try:
                    rows=supplement(args["kwargs"]["symbol"],rows)
                except Exception:
                    pass  # Keep the archive; missing target remains pending.
            if args["endpoint"].endswith("_index_ths"):
                from .ths_recent import supplement
                try:
                    rows=supplement(args["endpoint"],args["kwargs"],rows)
                except Exception:
                    # Preserve verified annual history. Caller still marks a missing target pending.
                    pass
        result = {"rows": rows}
    except Exception as exc:
        # Avoid persisting upstream URLs or credentials in diagnostics.
        from .normalize import PendingData
        result = {"pending": str(exc)} if isinstance(exc, PendingData) else {"error": type(exc).__name__}
    Path(response).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    child(*sys.argv[1:])
