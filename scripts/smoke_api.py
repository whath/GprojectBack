"""Start a real loopback HTTP server, verify auth/OpenAPI, then stop it."""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    token=secrets.token_urlsafe(32)
    environment={**os.environ,"API_TOKEN":token}
    process=subprocess.Popen([sys.executable,"-m","uvicorn","marketdata.api:app","--host","127.0.0.1","--port",str(port)],
                             env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    session=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base=f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with session.open(base+"/healthz",timeout=1) as response:
                    assert response.status==200
                break
            except (OSError,urllib.error.URLError):
                if process.poll() is not None:
                    raise RuntimeError("API process exited during startup")
                time.sleep(.1)
        else:
            raise RuntimeError("API startup timed out")
        try:
            session.open(base+"/v1/status",timeout=5)
            raise AssertionError("unauthenticated request unexpectedly succeeded")
        except urllib.error.HTTPError as exc:
            assert exc.code==401
        request=urllib.request.Request(base+"/v1/status",headers={"Authorization":"Bearer "+token})
        with session.open(request,timeout=5) as response:
            result=json.load(response)
        with session.open(base+"/openapi.json",timeout=5) as response:
            schema=json.load(response)
        Path("docs").mkdir(exist_ok=True)
        Path("docs/openapi.json").write_text(json.dumps(schema,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"http_smoke":"passed","unauthorized_status":401,
                          "business_status":200,"openapi_paths":len(schema["paths"]),
                          "stored_runs":len(result["runs"])},ensure_ascii=False))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__=="__main__":
    main()
