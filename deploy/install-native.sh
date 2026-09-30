#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
app_dir=$(pwd -P)
[ "$app_dir" = /opt/personal-market-data/app ] || { echo 'Extract release into /opt/personal-market-data/app first.' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo 'Run the installer as root.' >&2; exit 1; }
if ! id marketdata >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/personal-market-data --shell /sbin/nologin marketdata
fi
install -d -o marketdata -g marketdata -m 750 /var/lib/personal-market-data
python3 -m venv /opt/personal-market-data/venv
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}" /opt/personal-market-data/venv/bin/python -m pip install --prefer-binary --timeout 30 --retries 2 -r requirements.txt
/opt/personal-market-data/venv/bin/python -m pip check
if [ ! -f /etc/personal-market-data.env ]; then
    umask 077
    /opt/personal-market-data/venv/bin/python - <<'PY'
import secrets
from pathlib import Path
Path('/etc/personal-market-data.env').write_text('API_TOKEN='+secrets.token_urlsafe(48)+'\nDATA_DIR=/var/lib/personal-market-data\nCONFIG_PATH=/opt/personal-market-data/app/config/universe.json\nPYTHONUNBUFFERED=1\nPYTHONDONTWRITEBYTECODE=1\n',encoding='utf-8')
PY
fi
chmod 600 /etc/personal-market-data.env
for service in api worker; do
    if [ "$service" = api ]; then
        command='/opt/personal-market-data/venv/bin/python -m uvicorn marketdata.api:app --host 127.0.0.1 --port 8000 --workers 1'
        memory=384M
    else
        command='/opt/personal-market-data/venv/bin/python -m marketdata worker'
        memory=1000M
    fi
    cat > "/etc/systemd/system/personal-market-${service}.service" <<EOF
[Unit]
Description=Personal market data ${service}
Wants=network-online.target
After=network-online.target
[Service]
User=marketdata
Group=marketdata
WorkingDirectory=/opt/personal-market-data/app
EnvironmentFile=/etc/personal-market-data.env
ExecStart=$command
Restart=on-failure
RestartSec=15
MemoryMax=$memory
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/personal-market-data
[Install]
WantedBy=multi-user.target
EOF
done
systemctl daemon-reload
systemctl enable --now personal-market-api.service
echo 'API installed; worker remains stopped until cloud collection acceptance.'
