"""Export stored real bars and verify them through a temporary HTTP API."""
import csv
import hashlib
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

from marketdata import db
from marketdata.settings import data_dir


def main():
    with db.connect() as con:
        rows=[dict(r) for r in con.execute("SELECT * FROM bars ORDER BY instrument_id,trade_date")]
        instruments=[dict(r) for r in con.execute("SELECT * FROM instruments ORDER BY id")]
    if not rows:
        raise RuntimeError("no stored bars to export")
    folder=data_dir()/"export"
    folder.mkdir(exist_ok=True)
    (folder/"bars.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
    (folder/"instruments.json").write_text(json.dumps(instruments,ensure_ascii=False,indent=2),encoding="utf-8")
    with (folder/"bars.csv").open("w",encoding="utf-8-sig",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    token=secrets.token_urlsafe(32)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    process=subprocess.Popen([sys.executable,"-m","uvicorn","marketdata.api:app","--host","127.0.0.1","--port",str(port)],
        env={**os.environ,"API_TOKEN":token},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base=f"http://127.0.0.1:{port}"
    summary=[]
    try:
        for _ in range(100):
            try:
                with opener.open(base+"/healthz",timeout=1):
                    break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError("API exited")
                time.sleep(.1)
        else:
            raise RuntimeError("API startup timed out")
        def fetch(path,filename):
            request=urllib.request.Request(base+path,headers={"Authorization":"Bearer "+token})
            with opener.open(request,timeout=10) as response:
                assert response.status==200
                result=json.load(response)
            (folder/filename).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            return result
        for instrument in instruments:
            identifier=instrument["id"]
            expected=[r for r in rows if r["instrument_id"]==identifier]
            if not expected:
                continue
            response=fetch("/v1/bars/"+identifier+"?limit=2000",identifier+".api.json")
            assert response["items"]==expected[-2000:]
            summary.append({"instrument":identifier,"rows":len(expected),"start":expected[0]["trade_date"],
                            "end":expected[-1]["trade_date"],"latest_close":expected[-1]["close"],
                            "source":expected[-1]["source"],"api_http_status":200})
        day=max(r["trade_date"] for r in rows)
        fetch("/v1/us/sectors?trade_date="+day,"sectors.api.json")
        fetch("/v1/us/ai?trade_date="+day,"ai.api.json")
        fetch("/v1/status","status.api.json")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    manifest={"exported_at":db.now_iso(),"total_bars":len(rows),"sample_only":True,
              "instruments":summary,"files":{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(folder.iterdir()) if p.is_file() and p.name!="manifest.json"}}
    (folder/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False))


if __name__=="__main__":
    main()
