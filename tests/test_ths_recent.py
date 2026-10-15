import pytest
from marketdata.ths_recent import parse_recent,merge


def test_ths_recent_checked_overlap_and_no_history_overwrite():
    rows=parse_recent('quotebridge_v4_line_bk_881121_01_last({"data":"20260929,10,12,9,11,100,1000;20260930,11,13,10,12,200,2000"})')
    assert merge(rows[:1],rows,"2026-09-30")==rows
    changed=[dict(rows[0],收盘="10")]
    with pytest.raises(ValueError,match="conflict"):merge(changed,rows,"2026-09-30")
    with pytest.raises(ValueError,match="no overlap"):merge([],rows,"2026-09-30")
    with pytest.raises(ValueError):parse_recent('unexpected({"data":"x"})')


def test_current_ths_candle_requires_identity_date_close_and_previous_session(monkeypatch):
    import json
    from datetime import datetime
    import marketdata.ths_recent as ths
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            from zoneinfo import ZoneInfo
            return datetime(2026,10,15,19,tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(tz) if tz else datetime(2026,10,15,19)
    monkeypatch.setattr(ths,'datetime',Clock)
    point={'1':'20261015','7':'10','8':'12','9':'9','11':'11','13':100,'19':'1000','dt':'1505'}
    def wire(data,code='881121'):
        return f'quotebridge_v4_line_bk_{code}_01_today('+json.dumps({'bk_'+code:data})+')'
    history=[{'日期':'2026-10-14'}]
    row=ths.parse_today(wire(point),'881121','2026-10-15',history)
    assert row['开盘']=='10' and row['收盘']=='11' and row['source_frame']=='ths_v4_today'
    assert row['volume_unit']=='source_unit_unverified'
    for data in [dict(point,dt='1459'),dict(point,dt='2000'),dict(point,**{'1':'20261014'}),dict(point,**{'9':'12'})]:
        with pytest.raises(ValueError):ths.parse_today(wire(data),'881121','2026-10-15',history)
    with pytest.raises(ValueError):ths.parse_today(wire(point,'881122'),'881121','2026-10-15',history)
    with pytest.raises(ValueError):ths.parse_today(wire(point),'881121','2026-10-15',[])
