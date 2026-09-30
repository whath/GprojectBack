#!/bin/bash
set -euo pipefail
app=/opt/personal-market-data/app
stage=/opt/personal-market-data/quotes-stage
stamp=$(date +%Y%m%dT%H%M%S)
systemctl stop personal-market-worker personal-market-api
trap 'systemctl start personal-market-api personal-market-worker' EXIT
tar -czf "/opt/personal-market-data/before-quotes-$stamp.tar.gz" -C "$app" marketdata config
cd "$app"
export DATA_DIR=/var/lib/personal-market-data
/opt/personal-market-data/venv/bin/python -m marketdata backup "$DATA_DIR/backups/before-quotes-$stamp.sqlite3"
cp "$stage"/*.py "$app/marketdata/"
/opt/personal-market-data/venv/bin/python - <<'PY'
import json
from pathlib import Path
from marketdata import db
p=Path('config/universe.json')
cfg=json.loads(p.read_text())
cfg.update(cn_full_close_quotes=True,offsite_backup_expected=True,collection_budget_seconds=1800)
p.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
db.init()
PY
chown -R marketdata:marketdata "$app/marketdata" "$app/config"
systemctl start personal-market-api personal-market-worker
systemctl is-active personal-market-api personal-market-worker
