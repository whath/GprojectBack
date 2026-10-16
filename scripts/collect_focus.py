"""Explicit finite initialization for the selected publisher-specific topics."""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdata import db
from marketdata.board_data import collect_flow, current_members
from marketdata.calendars import CN, validate_collection
from marketdata.focus_boards import topics
from marketdata.history import sessions
from marketdata.pipeline import Collector, CollectorLock
from marketdata.public_feeds import PublicSource
from marketdata.settings import config

parser=argparse.ArgumentParser()
parser.add_argument('--date',type=date.fromisoformat,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--source',choices=('eastmoney','ths','all'),default='all')
args=parser.parse_args()
cfg=config();validate_collection('CN',args.date,cfg)
args.output.mkdir(parents=True,exist_ok=True)
settings=dict(cfg,request_attempts=1,request_timeout_seconds=40,raw_response_dir=str(args.output/'sdk-raw'))
collector=Collector('CN',args.date,settings=settings,refresh=True)
source=PublicSource(args.output/'public-raw')
expected=set(sessions('CN',args.date,8,cfg));results=[]
with CollectorLock():
    for topic in topics():
        if args.source!='all' and topic['classification_source']!=args.source:continue
        item=collector.item(topic['kind'],topic['symbol'],topic['source_name'],topic['symbol'])
        item['group_name']=topic['classification_source'];db.instrument(item)
        result=dict(id=item['id'],topic=topic['topic'])
        endpoint='stock_board_'+item['kind']+('_index_ths' if topic['classification_source']=='ths' else '_hist_em')
        try:
            if topic['classification_source']=='ths':
                from marketdata.selected_ths import collect
                collect(source,topic,args.date,cfg)
            else:collector.history(item,endpoint)
            with db.connect() as con:
                actual={row[0] for row in con.execute("SELECT trade_date FROM bars WHERE instrument_id=? AND trade_date>=? AND trade_date<=? AND adjustment='none'",(item['id'],min(expected),str(args.date)))}
            assert actual==expected,'seven-day ranking dates incomplete'
            result['history']='complete'
        except Exception as exc:result['history']=type(exc).__name__+': '+str(exc)[:120]
        if topic['classification_source']=='eastmoney':
            try:result['fund_rows']=collect_flow(source,item,args.date,cfg)
            except Exception as exc:result['fund_error']=type(exc).__name__
            try:
                members=current_members(source,item['symbol'])
                db.put_dataset('members:'+item['id'],'CN',datetime.now(CN).date().isoformat(),members)
                result['members']=len(members)
            except Exception as exc:result['members_error']=type(exc).__name__
        results.append(result)
        (args.output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
