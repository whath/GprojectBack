"""Dated selected concepts from the publisher's own verified index identity."""
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from . import db, normalize
from .history import sessions
from .ths_recent import merge, parse_recent, quote_headers, parse_today


def parse_archive(text,index,year):
    prefix=f'quotebridge_v4_line_bk_{index}_01_{year}('
    if not text.strip().startswith(prefix):raise ValueError('THS annual identity mismatch')
    payload=json.loads(text[text.index('(')+1:text.rindex(')')])
    # Same published seven-field layout as the last.js resource.
    rows=parse_recent('quotebridge_v4_line_bk_'+index+'_01_last('+json.dumps(payload)+')')
    if any(not row['日期'].startswith(str(year)+'-') for row in rows):raise ValueError('THS archive year mismatch')
    return rows


def collect(source,topic,target,cfg):
    symbol=topic['symbol'].removeprefix('THS_')
    if not re.fullmatch(r'\d{6}',symbol):raise ValueError('THS concept identity required')
    page=source.get(f'https://q.10jqka.com.cn/gn/detail/code/{symbol}/')
    node=BeautifulSoup(page,'html.parser').select_one('input#clid')
    index=node.get('value','') if node else ''
    if not re.fullmatch(r'\d{6}',index):raise ValueError('THS index identity absent')
    headers=quote_headers()
    def get(url):return source.get(url,headers=headers)
    expected=sessions('CN',target,30,cfg)
    annual=[]
    for year in sorted({int(expected[0][:4]),target.year}):
        annual+=parse_archive(get(f'https://d.10jqka.com.cn/v4/line/bk_{index}/01/{year}.js'),index,year)
    text=get(f'https://d.10jqka.com.cn/v4/line/bk_{index}/01/last.js')
    payload=json.loads(text[text.index('(')+1:text.rindex(')')])
    if payload.get('name')!=topic['source_name']:raise ValueError('THS published concept name mismatch')
    recent=parse_recent(text)
    from .calendars import CN
    current=target==datetime.now(CN).date()
    if current:
        annual=[row for row in annual if row['日期']<str(target)]
        recent=[row for row in recent if row['日期']<str(target)]
    rows=merge(annual,recent,str(target))
    if current:rows.append(parse_today(get(f'https://d.10jqka.com.cn/v4/line/bk_{index}/01/today.js'),index,str(target),rows))
    normalized=normalize.bars([row for row in rows if row['日期'] in expected],str(target),'concept','CN')
    if {row['trade_date'] for row in normalized}!=set(expected):raise normalize.PendingData('selected THS history incomplete')
    publisher='public/ths-selected-concept/v1'
    with db.connect() as con:
        old={row[0] for row in con.execute('SELECT DISTINCT source FROM bars WHERE instrument_id=?',(topic['id'],))}
        previous={row['trade_date']:dict(row) for row in con.execute('SELECT * FROM bars WHERE instrument_id=?',(topic['id'],))}
    if old=={'akshare/ths'}:
        overlap=[row for row in normalized if row['trade_date'] in previous]
        if not overlap:raise ValueError('existing THS series has no identity overlap')
        for row in overlap:
            before=previous[row['trade_date']]
            if any(abs(row[key]-before[key])>.001 for key in ('open','high','low','close')):
                raise ValueError('THS SDK/direct history conflict')
        publisher='akshare/ths'  # Same THS index, independently reconciled, preserve published identity.
    elif old and old!={publisher}:raise ValueError('THS publisher migration requires reconciliation')
    for row in normalized:row['source']=publisher
    db.put_bars(topic['id'],normalized)
    return len(normalized)
