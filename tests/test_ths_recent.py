import pytest
from marketdata.ths_recent import parse_recent,merge


def test_ths_recent_checked_overlap_and_no_history_overwrite():
    rows=parse_recent('quotebridge_v4_line_bk_881121_01_last({"data":"20260929,10,12,9,11,100,1000;20260930,11,13,10,12,200,2000"})')
    assert merge(rows[:1],rows,"2026-09-30")==rows
    changed=[dict(rows[0],收盘="10")]
    with pytest.raises(ValueError,match="conflict"):merge(changed,rows,"2026-09-30")
    with pytest.raises(ValueError,match="no overlap"):merge([],rows,"2026-09-30")
    with pytest.raises(ValueError):parse_recent('unexpected({"data":"x"})')
