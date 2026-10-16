"""User-selected topics, with explicit publisher taxonomy and distinct identities."""
TOPICS = [
    ('半导体设备','industry','BK1326','半导体设备','eastmoney'),
    ('存储','concept','BK1137','存储芯片','eastmoney'),
    ('封测','industry','BK1328','集成电路封测','eastmoney'),
    ('储能','concept','BK0989','储能概念','eastmoney'),
    ('AIDC','concept','THS_308642','数据中心(AIDC)','ths'),
    ('银行','industry','BK1283','银行','eastmoney'),
    ('游戏','industry','BK1046','游戏Ⅱ','eastmoney'),
    ('传媒','industry','BK0486','传媒','eastmoney'),
    ('证券','industry','BK0473','证券Ⅱ','eastmoney'),
    ('军工','industry','BK1204','国防军工','eastmoney'),
    ('数字货币','concept','BK0883','数字货币','eastmoney'),
    ('CPO','concept','BK1128','CPO概念','eastmoney'),
    ('PCB','concept','BK0877','PCB','eastmoney'),
    ('通信','industry','BK1215','通信','eastmoney'),
    ('旅游','industry','BK1272','旅游及景区','eastmoney'),
    ('煤炭','industry','BK0437','煤炭','eastmoney'),
    ('电力设备','industry','BK1200','电力设备','eastmoney'),
    ('医药','industry','BK1216','医药生物','eastmoney'),
    ('固态电池','concept','BK0968','固态电池','eastmoney'),
    ('机器人','concept','BK1090','机器人概念','eastmoney'),
    ('算力租赁','concept','THS_309068','算力租赁','ths'),
    ('数据中心','concept','BK0922','数据中心','eastmoney'),
]


def topics():
    return [dict(topic=topic,id=f'CN.{kind}.{symbol}',kind=kind,symbol=symbol,
        source_name=name,classification_source=source,order=index) for index,(topic,kind,symbol,name,source) in enumerate(TOPICS)]


def identifiers(kind=None,source=None):
    return {row['id'] for row in topics() if (kind is None or row['kind']==kind) and (source is None or row['classification_source']==source)}


def report(target,cfg):
    from .gainers import ranking
    from .board_data import report as flow_report
    from fastapi import HTTPException
    rows=[]
    for source in ('eastmoney','ths'):
        for kind in ('industry','concept'):
            wanted=identifiers(kind,source)
            if not wanted:continue
            result=ranking(target,cfg,kind,source,period=7,allowed_ids=wanted)
            by_id={row['id']:row for row in result['items']}
            for item in topics():
                if item['id'] not in wanted:continue
                row=dict(item,trade_date=str(target),status='not_collected',close=None,change_7d_pct=None,net_7d=None)
                if item['id'] in by_id:
                    row.update(by_id[item['id']]);row.update(name=item['topic'],source_name=item['source_name'],status='current')
                if source=='eastmoney':
                    try:
                        flow=flow_report(item['id'],7,target)
                        from .history import sessions
                        expected=set(sessions('CN',target,7,cfg))
                        if {point['trade_date'] for point in flow['items']}==expected:
                            row['net_7d']=sum(point['net'] for point in flow['items'])
                    except HTTPException as exc:
                        if exc.status_code!=503:raise
                rows.append(row)
    rows.sort(key=lambda row:row['order'])
    return dict(items=rows,total=len(rows),trade_date=str(target),period='7d',period_sessions=7,
        fund_unit='yuan',fund_currency='CNY',fund_definition='large and super-large order net; publisher convention',
        expected_count=len(TOPICS),available_count=sum(row['status']=='current' for row in rows),
        taxonomy='publisher-specific; indices and membership never merged across publishers',
        omitted_topics=[dict(topic='稳定币',reason='omitted by user; no independently verified index')],collection_on_read=False)
