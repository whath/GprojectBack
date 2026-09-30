#!/usr/bin/env bash
set -euo pipefail
cd /opt/personal-market-data/app
export DATA_DIR=/var/lib/personal-market-data CONFIG_PATH=/opt/personal-market-data/app/config/universe.json
py=/opt/personal-market-data/venv/bin/python
for attempt in $(seq 1 120); do
  busy=$($py -c "from marketdata import db; c=db.connect(); con=c.__enter__(); print(con.execute(\"SELECT count(*) FROM runs WHERE status='running'\").fetchone()[0]); c.__exit__(None,None,None)")
  if [ "$busy" = 0 ]; then break; fi
  sleep 5
done
if [ "$busy" != 0 ]; then echo 'Worker still collecting; no files replaced'; exit 1; fi
systemctl stop personal-market-worker personal-market-api
tar -xzf /tmp/personal-market-hardening-final.tar.gz -C /opt/personal-market-data/app
runuser -u marketdata -- env DATA_DIR="$DATA_DIR" CONFIG_PATH="$CONFIG_PATH" "$py" -m marketdata init
systemctl start personal-market-api
runuser -u marketdata -- env DATA_DIR="$DATA_DIR" CONFIG_PATH="$CONFIG_PATH" "$py" -m marketdata collect --market US --date 2026-09-29
systemctl start personal-market-worker
runuser -u marketdata -- env DATA_DIR="$DATA_DIR" CONFIG_PATH="$CONFIG_PATH" "$py" -m marketdata backup "$DATA_DIR/backups/hardening-acceptance.sqlite3"
systemctl is-active personal-market-api personal-market-worker
