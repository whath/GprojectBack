"""Supplement ETF historical archive with a dated, same-source closing quote."""
import re
from datetime import datetime,timedelta
from . import normalize
from .calendars import CN,session_close
from .settings import config


def merge(annual,text,symbol,day,previous_day):
    match=re.fullmatch(r'\s*var hq_str_'+re.escape(symbol)+r'="([^"]*)";\s*',text)
    if not match:raise ValueError("invalid Sina quote wrapper")
    fields=match[1].split(",")
    if len(fields)<32:raise ValueError("short Sina quote")
    stamp=datetime.fromisoformat(fields[30]+"T"+fields[31]).replace(tzinfo=CN)
    if stamp.date().isoformat()!=day or (stamp.hour,stamp.minute)<(15,0):return annual
    by_day={normalize.day_string(r["日期"]):r for r in annual}
    if day in by_day:return annual
    previous=by_day.get(previous_day)
    if not previous or abs(float(previous["收盘"])-float(fields[2]))>0.0001:
        raise ValueError("Sina previous close does not match verified archive")
    row=dict(zip(("日期","开盘","最高","最低","收盘","成交量","成交额"),
                 [day,fields[1],fields[4],fields[5],fields[3],fields[8],fields[9]]),volume_unit="source_unit_unverified")
    normalize.bars([row],day,"etf","CN")
    return annual+[row]


def supplement(symbol,annual):
    today=datetime.now(CN).date()
    if any(normalize.day_string(r["日期"])==today.isoformat() for r in annual):return annual
    if not re.fullmatch(r"(sh|sz)\d{6}",symbol):raise ValueError("invalid ETF source symbol")
    cfg=config()
    if session_close("CN",today,cfg) is None:return annual
    previous=today-timedelta(days=1)
    for _ in range(31):
        if session_close("CN",previous,cfg) is not None:break
        previous-=timedelta(days=1)
    import requests
    response=requests.get("https://hq.sinajs.cn/list="+symbol,headers={"Referer":"https://finance.sina.com.cn/"},timeout=15)
    response.raise_for_status()
    return merge(annual,response.content.decode("gb18030",errors="replace"),symbol,today.isoformat(),previous.isoformat())
