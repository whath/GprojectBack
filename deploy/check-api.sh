#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
docker compose -p personal-market-data exec -T api python - <<'PY'
import json,os,time,urllib.request,urllib.error
base='http://127.0.0.1:8000'
for attempt in range(30):
    try:
        with urllib.request.urlopen(base+'/healthz',timeout=3) as response:
            assert json.load(response)['status']=='ok'
        break
    except OSError:
        if attempt==29: raise
        time.sleep(1)
try:
    urllib.request.urlopen(base+'/v1/status',timeout=5)
    raise AssertionError('unauthenticated business request accepted')
except urllib.error.HTTPError as error:
    assert error.code==401
request=urllib.request.Request(base+'/v1/status',headers={'Authorization':'Bearer '+os.environ['API_TOKEN']})
with urllib.request.urlopen(request,timeout=5) as response:
    status=json.load(response)
print(json.dumps({'health':'passed','unauthorized_status':401,'authenticated_status':200,'runs':len(status['runs'])}))
PY
