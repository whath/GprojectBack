"""Explicit bounded ingestion for the revised business scope; never scheduled."""
from datetime import date

from .calendars import latest_ready
from .pipeline import CollectorLock
from .public_feeds import PublicSource


def run(cfg, features, day=None, max_items=10, scope='configured', all_boards=False, raw_dir=None):
    from . import macro_assets, calendar_feed, policy_news, board_data, gainers, important_calendar
    cfg=dict(cfg,raw_response_dir=str(raw_dir) if raw_dir else None)
    day=day or latest_ready('CN',cfg)
    source=PublicSource(raw_dir)
    results=[]
    with CollectorLock():
        if 'macro' in features:results.extend(macro_assets.collect(source))
        if 'calendar' in features:results.append(calendar_feed.collect(source,cfg))
        if 'important-calendar' in features:results.extend(important_calendar.collect(source,cfg))
        if 'policy' in features:results.extend(policy_news.collect(source,cfg))
        if 'priority-news' in features:
            from . import priority_news
            if 'policy' not in features:results.extend(policy_news.collect(source,cfg))
            results.extend(priority_news.collect(source,cfg))
        if 'boards' in features:
            try:results.extend(board_data.collect(source,cfg,day,max_items,all_boards=all_boards))
            except Exception as exc:results.append(dict(feature='boards',status='failed',error=type(exc).__name__))
        if 'gainers' in features:results.extend(gainers.collect(dict(cfg,raw_response_dir=str(raw_dir) if raw_dir else None),day,max_items,scope))
    return dict(trade_date=str(day),status='complete' if results and all(r['status']=='complete' for r in results) else 'partial',
                items=results,scope='explicit bounded manual collection',automatic_jobs_enabled=False)
