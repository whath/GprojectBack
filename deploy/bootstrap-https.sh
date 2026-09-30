#!/usr/bin/env bash
set -euo pipefail
test -x /usr/sbin/nginx
test -x /opt/personal-market-data/certbot-venv/bin/certbot
if [ ! -e /etc/nginx/nginx.conf.before-market ]; then
    cp -a /etc/nginx/nginx.conf /etc/nginx/nginx.conf.before-market
fi
install -d -m 755 /var/www/acme/.well-known/acme-challenge
cat >/etc/nginx/nginx.conf <<'CONF'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;
events { worker_connections 1024; }
http {
    types_hash_max_size 2048;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /var/log/nginx/access.log combined;
    sendfile on;
    keepalive_timeout 65;
    include /etc/nginx/conf.d/*.conf;
}
CONF
cat >/etc/nginx/conf.d/market-api.conf <<'CONF'
server {
    listen 80;
    server_name ericcself.top;
    server_tokens off;
    location /.well-known/acme-challenge/ { root /var/www/acme; }
    location / { return 404; }
}
CONF
printf 'market-acme-reachability\n' >/var/www/acme/.well-known/acme-challenge/reachability
nginx -t
systemctl enable --now nginx
