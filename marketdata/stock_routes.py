"""Preserve a verified Beijing series' publisher during subsequent captures."""
from . import db


def history_endpoint(item, primary='tencent'):
    if primary == 'eastmoney':
        return 'stock_zh_a_hist'
    if item['symbol'].startswith(('0', '3', '6')):
        return 'stock_zh_a_hist_tx'
    with db.connect() as con:
        sources = {r[0] for r in con.execute(
            "SELECT DISTINCT source FROM bars WHERE instrument_id=? AND adjustment='none'", (item['id'],))}
    # Existing Sina series remain continuous. The full-universe capture also
    # verifies Tencent Beijing series, which must not silently switch back.
    return 'stock_zh_a_hist_tx' if sources == {'akshare/tencent'} else 'stock_zh_a_daily'
