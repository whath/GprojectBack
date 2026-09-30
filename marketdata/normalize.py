import hashlib
import json
import math
from datetime import date


class PendingData(ValueError):
    """A response does not establish that the target date is complete."""


def number(value):
    if value is None or str(value).strip() in ("", "-", "--", "None", "nan"):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def day_string(value):
    text = str(value)[:10]
    return date.fromisoformat(text).isoformat()


def bars(rows, target, kind, market):
    result = []
    seen = set()
    for row in rows:
        day = day_string(row["日期"])
        if day > target:
            continue
        prices = {k: number(row[col]) for k, col in
                  (("open", "开盘"), ("high", "最高"), ("low", "最低"), ("close", "收盘"))}
        if any(v is None or v <= 0 for v in prices.values()):
            raise ValueError("missing or nonpositive OHLC")
        if prices["low"] > min(prices["open"], prices["close"]) or prices["high"] < max(prices["open"], prices["close"]):
            raise ValueError("inconsistent OHLC bounds")
        if day in seen:
            raise ValueError("duplicate daily bar")
        seen.add(day)
        volume = number(row.get("成交量"))
        if volume is not None and volume < 0:
            raise ValueError("negative volume")
        result.append(dict(trade_date=day, **prices, volume=volume,
                           volume_unit=row.get("volume_unit", "share" if market == "US" else ("lot_100_shares" if kind == "stock" else "source_unit_unverified")),
                           amount=number(row.get("成交额")), change_pct=number(row.get("涨跌幅"))))
    # Preserve valid history even when target is missing; caller publishes pending status.
    return sorted(result, key=lambda r: r["trade_date"])


def lhb(rows, target, institutions=False):
    result = {}
    for row in rows:
        day = day_string(row.get("上榜日期") if institutions else row.get("上榜日"))
        if day != target:
            raise ValueError("LHB returned unexpected trade date")
        symbol = str(row["代码"]).zfill(6)
        reason = str(row["上榜原因"])
        period = "multi_day" if any(v in reason for v in ("连续", "累计", "累积", "三个", "3个")) else "single_day"
        key = hashlib.sha256(f"{symbol}|{day}|{reason}".encode()).hexdigest()
        prefix = "机构" if institutions else "龙虎榜"
        clean = dict(key=key, symbol=symbol, name=row["名称"], trade_date=day,
                     reason=reason, period=period,
                     buy_amount=number(row.get(prefix + ("买入总额" if institutions else "买入额"))),
                     sell_amount=number(row.get(prefix + ("卖出总额" if institutions else "卖出额"))),
                     net_amount=number(row.get("机构买入净额" if institutions else "龙虎榜净买额")),
                     currency="CNY", amount_unit="yuan", source="akshare/eastmoney")
        # Do not expose future-return fields from historical LHB API in an as-of report.
        if key in result and result[key] != clean:
            raise ValueError("conflicting duplicate LHB reason")
        result[key] = clean
    if not result:
        raise PendingData("empty LHB response: publication/no-record state unverified")
    return list(result.values())


def seats(rows, symbol, direction):
    result = {}
    for row in rows:
        seat = str(row["交易营业部名称"])
        reason = str(row["类型"])
        key = hashlib.sha256(f"{symbol}|{direction}|{reason}|{row.get('序号')}|{seat}".encode()).hexdigest()
        result[key] = {"key": key, "symbol": symbol, "direction": direction, "seat": seat,
                       "reason": reason, "buy_amount": number(row.get("买入金额")),
                       "sell_amount": number(row.get("卖出金额")), "net_amount": number(row.get("净额")),
                       "amount_unit": "yuan", "source": "akshare/eastmoney"}
    if not result:
        raise PendingData("empty seat response")
    return list(result.values())


def members(rows):
    result = []
    for row in rows:
        code = str(row["代码"]).zfill(6)
        result.append({"key": code, "symbol": code, "name": row["名称"],
                       "as_of_type": "observed_current_membership"})
    if not result:
        raise PendingData("empty board membership")
    return result
