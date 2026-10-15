"""Read-only desktop client. Credentials come from environment variables."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class APIError(RuntimeError):
    def __init__(self, status, detail, retry_after=None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {detail}")


class MarketClient:
    def __init__(self, token, base="https://ericcself.top", timeout=20):
        if not base.startswith("https://") and not base.startswith("http://127.0.0.1:"):
            raise ValueError("Use HTTPS or a local SSH tunnel")
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        self.http = urllib.request.build_opener()

    def get(self, path, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(
            self.base + path + ("?" + query if query else ""),
            headers={"Authorization": "Bearer " + self.token},
        )
        try:
            with self.http.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error).get("detail", "Request failed")
            except (ValueError, AttributeError):
                detail = "Request failed"
            raise APIError(error.code, detail, error.headers.get("Retry-After")) from None

    def instruments(self, market="CN", kind="stock", page_size=500):
        offset, snapshot_id = 0, None
        while True:
            page = self.get("/v1/instruments", market=market, kind=kind,
                            limit=page_size, offset=offset, snapshot_id=snapshot_id)
            snapshot_id = page["snapshot_id"]
            yield from page["items"]
            offset += len(page["items"])
            if offset >= page["total"]:
                return
            if not page["items"]:
                raise RuntimeError("Incomplete pagination")


if __name__ == "__main__":
    client = MarketClient(os.environ["MARKET_API_TOKEN"], os.getenv("MARKET_API_BASE_URL", "https://ericcself.top"))
    print(json.dumps(client.get("/v1/deployment-status"), ensure_ascii=False, indent=2))
    # Supply explicit dates while collection is paused; do not silently relabel old data.
    result = client.get("/v1/bars/CN.stock.000001", start="2026-10-09", end="2026-10-14", limit=250)
    print(json.dumps({"instrument": result["instrument"]["id"], "bars": len(result["items"]),
                      "latest_date": result["items"][-1]["trade_date"] if result["items"] else None}, indent=2))
