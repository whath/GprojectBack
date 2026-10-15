import pytest
from marketdata import db


@pytest.fixture(autouse=True)
def isolated_data(tmp_path,monkeypatch):
    monkeypatch.setenv("DATA_DIR",str(tmp_path / "data"))
    monkeypatch.setenv("API_TOKEN","test-secret-token-that-is-at-least-32-characters")
    db.init()


@pytest.fixture
def event_clock(monkeypatch):
    """Event fixtures describe October 2026; retention must not use wall-clock today."""
    from datetime import datetime, timezone
    import marketdata.events as events
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value=datetime(2026,10,4,tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(events,"datetime",FixedDatetime)
