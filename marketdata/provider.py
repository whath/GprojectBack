"""Bound each AKShare call by an OS-process timeout (including pagination)."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ALLOWED = {
    "stock_info_sh_name_code", "stock_info_sz_name_code", "stock_info_bj_name_code",
    "stock_info_sh_delist", "stock_info_sz_delist",
    "stock_tfp_em", "news_economic_baidu",
    "stock_individual_fund_flow", "stock_news_em", "stock_fund_flow_industry",
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


def check_lhb_publication(payload):
    """Validate the response already obtained by AKShare; never issue a second fetch."""
    from .normalize import PendingData
    if payload.get("code") == 9201 and payload.get("success") is False and payload.get("result") is None:
        raise PendingData("source report empty (9201): no-disclosure state unverified")
    if payload.get("success") is not True or not isinstance(payload.get("result"),dict):
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
                request.write_text(json.dumps({"endpoint": endpoint, "kwargs": kwargs,
                    "transport": self.config.get("akshare_transport", "requests")}), encoding="utf-8")
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
    import requests
    original_get_before=requests.get
    original_init_before=requests.sessions.Session.__init__
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
        if args.get("transport") == "curl_cffi":
            import requests
            from curl_cffi import requests as curl_requests
            def transport_get(url, **kwargs):
                kwargs.setdefault("timeout", 15)
                kwargs["impersonate"] = "chrome"
                with curl_requests.Session(trust_env=os.getenv("MARKET_DIRECT_REQUESTS") != "1") as session:
                    return session.get(url, **kwargs)
            # The AKShare function, URL, parameters and parsing remain the same.
            # Only HTTP transport changes inside this disposable process.
            requests.get = transport_get
        elif args.get("transport", "requests") != "requests":
            raise ValueError("unsupported AKShare transport")
        if args["endpoint"] in ("stock_lhb_detail_em","stock_lhb_jgmmtj_em"):
            import requests
            original_get=requests.get
            def lhb_get(url, **kwargs):
                reply=original_get(url,**kwargs)
                reply.raise_for_status()
                check_lhb_publication(reply.json())
                return reply
            requests.get=lhb_get
        if args["endpoint"] == "cn_suspensions":
            from .suspensions import fetch
            rows = fetch(**args["kwargs"])
        elif args["endpoint"] == "cn_close_quotes":
            from .quotes import fetch
            rows = fetch(**args["kwargs"])
        elif args["endpoint"] == "us_watchlist_catalog":
            symbols = args["kwargs"]["symbols"]
            if len(symbols)>100 or any(not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in s) for s in symbols):
                raise ValueError("invalid or oversized US symbol batch")
            frame = ak.stock_us_spot_em()
            rows = [{"代码":str(r["代码"]),"名称":r["名称"]}
                    for r in frame.to_dict("records") if str(r["代码"]).split(".",1)[-1] in symbols]
        else:
            frame = getattr(ak, args["endpoint"])(**args["kwargs"])
            frame = adapt_frame(args["endpoint"], frame, args["kwargs"])
            # pandas safely converts NaN/NaT/numpy values; no pickle across processes.
            rows = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
        result = {"rows": rows}
    except Exception as exc:
        # Avoid persisting upstream URLs or credentials in diagnostics.
        from .normalize import PendingData
        result = {"pending": str(exc)} if isinstance(exc, PendingData) else {"error": type(exc).__name__}
    finally:
        requests.get=original_get_before
        requests.sessions.Session.__init__=original_init_before
    Path(response).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    child(*sys.argv[1:])
