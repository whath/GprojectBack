#!/usr/bin/env bash
# Run after extracting the release into its application directory.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
command -v docker >/dev/null || { echo 'Docker is required; inspect the host before installing it.' >&2; exit 1; }
docker compose version >/dev/null
if [ ! -f .env ]; then
    umask 077
    api_token=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    printf 'API_TOKEN=%s\nDATA_DIR=/data\nCONFIG_PATH=/app/config/universe.json\n' "$api_token" > .env
    unset api_token
fi
chmod 600 .env
if grep -q 'replace-with-' .env; then
    echo 'Existing .env contains a placeholder token; configure it before startup.' >&2
    exit 1
fi
docker compose -p personal-market-data build api
docker compose -p personal-market-data up -d api
echo 'API started on server loopback port 8000. Run deploy/check-api.sh before collecting samples.'
echo 'Worker is not started until cloud data validation completes.'
