import json
import os
from pathlib import Path


def data_dir():
    path = Path(os.getenv("DATA_DIR", "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def config():
    path = Path(os.getenv("CONFIG_PATH", "config/universe.json"))
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
