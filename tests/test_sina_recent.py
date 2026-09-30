import pytest
from marketdata.sina_recent import merge


def test_sina_supplement_requires_previous_session_and_matching_price():
    fields=[""]*32
    for i,v in {1:"4.421",2:"4.416",3:"4.432",4:"4.444",5:"4.415",8:"495854381",9:"2196229027",30:"2026-09-30",31:"15:34:59"}.items():fields[i]=v
    text='var hq_str_sh510300="'+','.join(fields)+'";'
    old=[{"日期":"2026-09-29","收盘":"4.416"}]
    assert merge(old,text,"sh510300","2026-09-30","2026-09-29")[-1]["收盘"]=="4.432"
    with pytest.raises(ValueError):merge([],text,"sh510300","2026-09-30","2026-09-29")
    with pytest.raises(ValueError):merge([dict(old[0],收盘="4.3")],text,"sh510300","2026-09-30","2026-09-29")
