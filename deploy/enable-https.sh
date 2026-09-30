#!/usr/bin/env bash
set -euo pipefail
test -s /etc/letsencrypt/live/ericcself.top/fullchain.pem
test -s /etc/letsencrypt/live/ericcself.top/privkey.pem
if [ ! -e /etc/nginx/market-api.before-https ]; then
    cp -a /etc/nginx/conf.d/market-api.conf /etc/nginx/market-api.before-https
fi
install -m 644 /opt/personal-market-data/app/deploy/ericcself.top.nginx.conf /etc/nginx/conf.d/market-api.conf
nginx -t
systemctl reload nginx
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat >/etc/letsencrypt/renewal-hooks/deploy/market-nginx <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
/usr/sbin/nginx -t
/bin/systemctl reload nginx
cd /opt/personal-market-data/app
runuser -u marketdata -- env DATA_DIR=/var/lib/personal-market-data /opt/personal-market-data/venv/bin/python -c "from marketdata.operations import alert; alert('tls_renewal_failed')"
HOOK
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/market-nginx
cat >/etc/systemd/system/market-certbot-renew.service <<'UNIT'
[Unit]
Description=Renew market API TLS certificate
OnFailure=market-certbot-alert.service
After=network-online.target nginx.service
Wants=network-online.target
[Service]
Type=oneshot
WorkingDirectory=/opt/personal-market-data/app
ExecStart=/opt/personal-market-data/certbot-venv/bin/certbot renew --quiet
UNIT
cat >/etc/systemd/system/market-certbot-alert.service <<'UNIT'
[Unit]
Description=Publish certificate renewal failure to dashboard
[Service]
Type=oneshot
User=marketdata
WorkingDirectory=/opt/personal-market-data/app
Environment=DATA_DIR=/var/lib/personal-market-data
ExecStart=/opt/personal-market-data/venv/bin/python -c "from marketdata.operations import alert; alert('tls_renewal_failed','Certificate renewal failed; inspect market-certbot-renew.service','error')"
UNIT
cat >/etc/systemd/system/market-certbot-renew.timer <<'UNIT'
[Unit]
Description=Check market API TLS renewal twice daily
[Timer]
OnCalendar=*-*-* 03,15:30:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now market-certbot-renew.timer
openssl x509 -in /etc/letsencrypt/live/ericcself.top/fullchain.pem -noout -dates -issuer -subject
