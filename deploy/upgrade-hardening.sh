#!/usr/bin/env bash
set -euo pipefail
cd /opt/personal-market-data/app
stamp=$(date +%Y%m%d-%H%M%S)
systemctl stop personal-market-worker personal-market-api
tar -czf "/opt/personal-market-data/app-before-hardening-$stamp.tar.gz" marketdata config deploy
export DATA_DIR=/var/lib/personal-market-data
export CONFIG_PATH=/opt/personal-market-data/app/config/universe.json
runuser -u marketdata -- env DATA_DIR="$DATA_DIR" CONFIG_PATH="$CONFIG_PATH" /opt/personal-market-data/venv/bin/python -m marketdata backup "$DATA_DIR/backups/before-hardening-$stamp.sqlite3"
tar -xzf /tmp/personal-market-hardening.tar.gz -C /opt/personal-market-data/app
install -m 644 /tmp/cloud-universe-hardening.json "$CONFIG_PATH"
runuser -u marketdata -- env DATA_DIR="$DATA_DIR" CONFIG_PATH="$CONFIG_PATH" /opt/personal-market-data/venv/bin/python -m marketdata init
systemctl start personal-market-api
echo "UPGRADE_INSTALLED; worker remains stopped for source rebuild"
