import pytest
from marketdata import db


@pytest.fixture(autouse=True)
def isolated_data(tmp_path,monkeypatch):
    monkeypatch.setenv("DATA_DIR",str(tmp_path / "data"))
    monkeypatch.setenv("API_TOKEN","test-secret-token-that-is-at-least-32-characters")
    db.init()
