#!/bin/bash
set -euo pipefail
cd /opt/personal-market-data/app
export DATA_DIR=/var/lib/personal-market-data
python=/opt/personal-market-data/venv/bin/python
# Refuse to interrupt an active collection. Caller retries after it finishes.
"$python" - <<'PY'
from marketdata import db
with db.connect() as con:
    if con.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0]:
        raise SystemExit('collection still active; defer finalization')
PY
systemctl stop personal-market-worker
trap 'systemctl start personal-market-worker' EXIT
"$python" -m marketdata backup /var/lib/personal-market-data/backups/before-expansion-final.sqlite3
for src in /opt/personal-market-data/quotes-stage/*.py; do
    name=$(basename "$src")
    install -o marketdata -g marketdata -m 644 "$src" "marketdata/$name.new"
    mv "marketdata/$name.new" "marketdata/$name"
done
"$python" - <<'PY'
import json
from pathlib import Path
from marketdata import db
from marketdata.calendars import CN
from datetime import datetime
p=Path('config/universe.json');cfg=json.loads(p.read_text())
cfg.update(cn_history_backfill=True,history_backfill_seconds=120,history_backfill_attempts=3)
p.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
# Bring forward an already-authorized bounded retry after the source fix; no budget reset.
with db.connect() as con:
    row=con.execute("SELECT slot,attempts FROM schedule_slots WHERE slot LIKE ? AND status='partial' ORDER BY slot DESC LIMIT 1",('CN:'+datetime.now(CN).date().isoformat()+':%',)).fetchone()
    if row and row['attempts']<1+cfg.get('retry_max_attempts',3):
        con.execute('UPDATE schedule_slots SET next_retry_at=? WHERE slot=?',(db.now_iso(),row['slot']))
PY
systemctl restart personal-market-api
systemctl start personal-market-worker
systemctl is-active personal-market-api personal-market-worker
