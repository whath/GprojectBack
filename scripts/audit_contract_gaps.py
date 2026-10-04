"""Verify repairs using temporary data; retain the original baseline evidence separately."""
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from marketdata import db, normalize
from marketdata.api import app
from marketdata.pipeline import Collector
from marketdata.settings import config


def raw(day, amount=1000, unit="share"):
    return {"日期": day, "开盘": 10, "最高": 11, "最低": 9,
            "收盘": 10, "成交量": 100, "成交额": amount, "volume_unit": unit}


def instrument(symbol):
    return dict(id=f"CN.stock.{symbol}", market="CN", kind="stock", symbol=symbol,
                name="audit fixture", source_code="sz" + symbol, currency="CNY")


def audit():
    evidence = {}
    with tempfile.TemporaryDirectory(prefix="gproject-contract-audit-") as folder:
        old_data, old_token = os.environ.get("DATA_DIR"), os.environ.get("API_TOKEN")
        os.environ["DATA_DIR"] = folder
        os.environ["API_TOKEN"] = "isolated-audit-token-at-least-32-characters"
        try:
            db.init()
            with TestClient(app) as client:
                client.headers["Authorization"] = "Bearer " + os.environ["API_TOKEN"]
                evidence["missing_routes"] = {
                    path: client.get(path).status_code for path in (
                        "/v1/cn/boards/CN.industry.THS_881121/flows?limit=20&classification_source=ths",
                        "/v1/cn/stocks/CN.stock.000001/flows?end=2026-09-30&limit=2000",
                        "/v1/events?limit=100")}
                item = instrument("000001")
                db.instrument(item)
                with db.connect() as con:
                    con.execute("""INSERT INTO tasks(market,trade_date,scope,task_key,status,message,updated_at)
                        VALUES('CN','2026-09-30','configured','bar:CN.stock.000001','failed','ProviderError',?)""",
                        (db.now_iso(),))
                empty = client.get("/v1/bars/CN.stock.000001?end=2026-09-30")
                evidence["failed_history_response"] = {"http_status": empty.status_code, "body": empty.json()}
                for name,rows in (("negative_amount",[raw("2026-09-29",-100)]),
                                  ("non_session",[raw("2026-09-27")]),
                                  ("mixed_volume_units",[raw("2026-09-28",unit="lot_100_shares"),raw("2026-09-29")])):
                    try:
                        normalize.bars(rows,"2026-09-30","stock","CN")
                        evidence[name]="accepted"
                    except ValueError as exc:
                        evidence[name]="rejected: "+str(exc)
                db.put_bars(item["id"],normalize.bars([raw("2026-09-29")],"2026-09-30","stock","CN"))
                # A new earlier ID between pages shifts OFFSET and duplicates an existing ID.
                db.instrument(instrument("000003"))
                page1 = client.get("/v1/instruments?market=CN&kind=stock&limit=1&offset=0").json()
                db.instrument(instrument("000000"))
                page2 = client.get("/v1/instruments?market=CN&kind=stock&limit=1&offset=1").json()
                evidence["pagination_during_update"] = {
                    "totals": [page1["total"], page2["total"]],
                    "ids": [page1["items"][0]["id"], page2["items"][0]["id"]]}
                # Simulate an imported bad row to establish the API's missing default cutoff.
                with db.connect() as con:
                    con.execute("INSERT INTO bars SELECT instrument_id,'2099-01-05',adjustment,open,high,low,close,volume,volume_unit,amount,change_pct,source,collected_at FROM bars WHERE instrument_id=?",(item["id"],))
                evidence["future_stored_row_returned_without_end"] = client.get("/v1/bars/CN.stock.000001").json()["items"][-1]["trade_date"]
                members = client.get("/v1/cn/boards/CN.industry.THS_881121/members?observed_date=2026-09-30").json()
                evidence["missing_members_snapshot"] = members

            with db.connect() as con:
                con.execute("DELETE FROM bars")
            history_rows = normalize.bars([raw("2026-09-29")], "2026-09-30", "stock", "CN")
            history_rows[0]["source"] = "akshare/tencent"
            db.put_bars(item["id"], history_rows)
            class CaptureProvider:
                def call(self, endpoint, **kwargs):
                    evidence["expanded_history_request"] = {"endpoint": endpoint, **kwargs}
                    return [raw("2026-09-30")]
            cfg = config()
            cfg["history_days"] = 4000
            collector = Collector("CN", date(2026, 9, 30), settings=cfg, provider=CaptureProvider())
            collector.call = collector.provider.call  # Avoid real-time collection gate for a fixture.
            collector.history(item, "stock_zh_a_hist_tx")
            evidence["wrong_currency_rejected"] = False
            try:
                db.instrument(dict(item,currency="USD"))
            except ValueError:
                evidence["wrong_currency_rejected"] = True
            with db.connect() as con:
                con.execute("UPDATE instruments SET currency='USD' WHERE id=?",(item["id"],))
            db.instrument(item)
            with db.connect() as con:
                evidence["catalog_refresh_does_not_correct_currency"] = con.execute(
                    "SELECT currency FROM instruments WHERE id=?", (item["id"],)).fetchone()[0]
            assert set(evidence["missing_routes"].values()) == {501}
            assert evidence["failed_history_response"]["http_status"] == 503
            assert evidence["negative_amount"].startswith("rejected")
            assert evidence["expanded_history_request"]["start_date"] < "20260919"
            assert evidence["pagination_during_update"]["ids"][0] != evidence["pagination_during_update"]["ids"][1]
        finally:
            for key, previous in (("DATA_DIR", old_data), ("API_TOKEN", old_token)):
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous
    return evidence


if __name__ == "__main__":
    result = audit()
    destination = Path("docs/CONTRACT-REPAIR-EVIDENCE-20261004.json")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
