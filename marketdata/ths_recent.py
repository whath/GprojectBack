"""Same-provider recent history supplements a lagging THS annual archive."""
import json
import re
from datetime import datetime

from . import normalize


def parse_recent(text):
    if not re.match(r"^\s*quotebridge_v4_line_bk_\d+_01_last\(",text):
        raise ValueError("unexpected THS response wrapper")
    payload=json.loads(text[text.index("(")+1:text.rindex(")")])
    rows=[]
    for entry in payload["data"].split(";"):
        fields=entry.split(",")
        if len(fields)<7:raise ValueError("invalid THS daily record")
        rows.append(dict(zip(("日期","开盘","最高","最低","收盘","成交量","成交额"),
            [datetime.strptime(fields[0],"%Y%m%d").date().isoformat(),*fields[1:7]]),volume_unit="source_unit_unverified"))
    return rows


def merge(annual,recent,target):
    old={normalize.day_string(r["日期"]):r for r in annual}
    new={normalize.day_string(r["日期"]):r for r in recent}
    overlap=set(old)&set(new)
    if not overlap:raise ValueError("THS recent history has no overlap for identity verification")
    for day in overlap:
        for key in ("开盘","最高","最低","收盘"):
            if abs(float(old[day][key])-float(new[day][key]))>0.001:
                raise ValueError("THS recent/annual OHLC conflict")
    # Validate all incoming OHLC before supplementing, never silently replace old history.
    normalize.bars(recent,target,"industry","CN")
    return annual+[new[d] for d in sorted(set(new)-set(old)) if d<=target]


def supplement(endpoint,kwargs,annual):
    target=datetime.strptime(kwargs["end_date"],"%Y%m%d").date().isoformat()
    if any(normalize.day_string(r["日期"])==target for r in annual):return annual
    import akshare as ak
    import requests
    headers={"User-Agent":"Mozilla/5.0","Referer":"https://q.10jqka.com.cn/"}
    if endpoint=="stock_board_industry_index_ths":
        catalog=ak.stock_board_industry_name_ths()
        codes=catalog.loc[catalog["name"]==kwargs["symbol"],"code"].tolist()
        if len(codes)!=1:raise ValueError("ambiguous THS board")
        code=str(codes[0])
    else:
        catalog=ak.stock_board_concept_name_ths()
        codes=catalog.loc[catalog["name"]==kwargs["symbol"],"code"].tolist()
        if len(codes)!=1:raise ValueError("ambiguous THS concept")
        response=requests.get(f"https://q.10jqka.com.cn/gn/detail/code/{codes[0]}",headers=headers,timeout=15)
        response.raise_for_status()
        from bs4 import BeautifulSoup
        element=BeautifulSoup(response.text,"lxml").find("input",id="clid")
        if element is None:raise ValueError("THS concept index identifier unavailable")
        code=element["value"]
    if not re.fullmatch(r"\d{6}",code):raise ValueError("invalid THS index identifier")
    response=requests.get(f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/last.js",headers=headers,timeout=15)
    response.raise_for_status()
    return merge(annual,parse_recent(response.text),target)
