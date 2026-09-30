from marketdata.suspensions import full_session


def test_only_full_session_suspension_counts():
    base={"SECURITY_CODE":"000016","SECURITY_NAME_ABBR":"test","SUSPEND_REASON":"notice",
          "SUSPEND_START_TIME":"2026-09-04 09:30:00","SUSPEND_END_TIME":None,"PREDICT_RESUME_DATE":None}
    assert len(full_session([base],"2026-09-30"))==1
    assert full_session([dict(base,SUSPEND_START_TIME="2026-09-30 09:34:00")],"2026-09-30")==[]
    assert full_session([dict(base,SUSPEND_END_TIME="2026-09-29 15:00:00",PREDICT_RESUME_DATE="2026-09-30 00:00:00")],"2026-09-30")==[]
