"""Same-provider recent history supplements a lagging THS annual archive."""
import json
import re
from datetime import datetime,timedelta,time

from . import normalize

BROWSER_UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36'


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


def parse_today(text,code,target,history):
    """Publisher's separately dated current candle; only after its market close."""
    if not re.match(r'^\s*quotebridge_v4_line_bk_'+re.escape(code)+r'_01_today\(',text):
        raise ValueError('unexpected THS current candle identity')
    payload=json.loads(text[text.index('(')+1:text.rindex(')')])
    point=payload['bk_'+code]
    day=datetime.strptime(str(point['1']),'%Y%m%d').date()
    if day.isoformat()!=target:
        raise normalize.PendingData('THS current candle is not the requested date')
    clock=datetime.strptime(str(point['dt']),'%H%M').time()
    if clock<time(15):
        raise normalize.PendingData('THS current candle does not confirm a post-close update')
    from .calendars import CN,session_close
    from .settings import config
    now=datetime.now(CN)
    if day>now.date() or (day==now.date() and now.time()<time(15,10)):
        raise normalize.PendingData('THS current candle is not ready for EOD')
    if day==now.date() and clock>now.time():
        raise normalize.PendingData('THS current candle source clock is in the future')
    previous=day-timedelta(days=1)
    for _ in range(31):
        if session_close('CN',previous,config()) is not None:break
        previous-=timedelta(days=1)
    if not any(normalize.day_string(r['日期'])==previous.isoformat() for r in history):
        raise ValueError('THS current candle requires a verified previous session')
    row=dict(日期=target,开盘=point['7'],最高=point['8'],最低=point['9'],收盘=point['11'],
             成交量=point['13'],成交额=point['19'],volume_unit='source_unit_unverified',
             source_observed_clock=str(point['dt']),source_frame='ths_v4_today')
    normalize.bars([row],target,'industry','CN')
    return row


def quote_headers():
    # Reuse the locked AKShare adapter's anonymous public-quote request format.
    from akshare.stock_feature.stock_board_industry_ths import _get_file_content_ths,py_mini_racer
    engine=py_mini_racer.MiniRacer()
    engine.eval(_get_file_content_ths('ths.js'))
    return {'User-Agent':BROWSER_UA,
            'Referer':'http://q.10jqka.com.cn','Host':'d.10jqka.com.cn','Cookie':'v='+engine.call('v')}


def supplement(endpoint,kwargs,annual):
    target=datetime.strptime(kwargs["end_date"],"%Y%m%d").date().isoformat()
    from .calendars import CN
    current=target==datetime.now(CN).date().isoformat()
    if not current and any(normalize.day_string(r["日期"])==target for r in annual):return annual
    import akshare as ak
    import requests
    headers={"User-Agent":BROWSER_UA,"Referer":"https://q.10jqka.com.cn/"}
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
    headers=quote_headers()
    response=requests.get(f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/last.js",headers=headers,timeout=15)
    response.raise_for_status()
    recent=parse_recent(response.text)
    # A dated current-year archive row may still be an intraday cached candle.
    # Pin historical identity independently; the explicitly post-close current
    # candle can revise today only. db.put_bars retains the previous version.
    if current:
        history=merge([r for r in annual if normalize.day_string(r['日期'])<target],
                      [r for r in recent if normalize.day_string(r['日期'])<target],target)
    else:
        return merge(annual,recent,target)
    response=requests.get(f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/today.js",headers=headers,timeout=15)
    response.raise_for_status()
    quote=parse_today(response.text,code,target,history)
    old=next((r for r in annual if normalize.day_string(r['日期'])==target),None)
    if old and abs(float(old['开盘'])-float(quote['开盘']))>0.001:
        raise ValueError('THS current/archive opening value conflict')
    return history+[quote]
