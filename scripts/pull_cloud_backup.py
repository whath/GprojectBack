"""Consistent SSH backup, SHA-256 verification and isolated SQLite restore rehearsal."""
import base64
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    cfg=json.loads((ROOT/"data/deploy/cloud-connection.json").read_text(encoding="utf-8"))
    destination=ROOT/"data/offsite-backups"
    destination.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename="cloud-"+stamp+".sqlite3"
    remote=cfg["remote_data"]+"/backups/"+filename
    options=["-i",cfg["key_path"],"-o","BatchMode=yes","-o","IdentitiesOnly=yes","-o","StrictHostKeyChecking=yes","-o","ConnectTimeout=15"]
    target=cfg["user"]+"@"+cfg["host"]
    def remote_python(code):
        script="import base64;exec(base64.b64decode("+repr(base64.b64encode(code.encode()).decode())+"))"
        command="cd "+shlex.quote(cfg["remote_app"])+" && DATA_DIR="+shlex.quote(cfg["remote_data"])+" "+shlex.quote(cfg["remote_python"])+" -c "+shlex.quote(script)
        result=subprocess.run(["ssh",*options,target,command],capture_output=True,text=True,timeout=180)
        if result.returncode:raise RuntimeError("remote backup operation failed; inspect SSH connectivity")
        return result.stdout
    meta=json.loads(remote_python(f"""import os,json,hashlib
from pathlib import Path
from marketdata import db
os.umask(0o077)
p=Path({remote!r})
db.backup(p)
with p.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
print(json.dumps(dict(sha256=digest,bytes=p.stat().st_size,created_at=db.now_iso())))
"""))
    partial=destination/(filename+".partial")
    try:
        result=subprocess.run(["scp",*options,target+":"+remote,str(partial)],capture_output=True,timeout=180)
        if result.returncode:raise RuntimeError("backup download failed")
        with partial.open("rb") as handle:digest=hashlib.file_digest(handle,"sha256").hexdigest()
        if digest!=meta["sha256"] or partial.stat().st_size!=meta["bytes"]:
            raise RuntimeError("backup size or SHA-256 mismatch")
        with tempfile.TemporaryDirectory(prefix="market-restore-") as folder:
            restored=Path(folder)/"restored.sqlite3"
            shutil.copy2(partial,restored)
            con=sqlite3.connect(restored)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise RuntimeError("restore integrity failed")
                meta["restored_counts"]={table:con.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in ("bars","datasets","instruments")}
            finally:con.close()
        final=destination/filename
        partial.replace(final)
        meta.update(file=filename,restore_verified=True,verified_at=datetime.now(timezone.utc).isoformat())
        final.with_suffix(".json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
        (destination/"latest.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
        remote_python(f"""import json,os
from pathlib import Path
from marketdata.operations import alert
p=Path({cfg['remote_data']!r})/'offsite-backup.json'
p.write_text({json.dumps(meta)!r},encoding='utf-8')
os.chmod(p,0o644)
alert('offsite_backup_failed')
Path({remote!r}).unlink()
""")
        # Only script-owned backups are rotated, after both verification steps succeed.
        for old in sorted(destination.glob("cloud-????????T??????Z.sqlite3"),reverse=True)[30:]:
            old.unlink();old.with_suffix(".json").unlink(missing_ok=True)
        print(json.dumps(meta))
    finally:
        partial.unlink(missing_ok=True)


if __name__=="__main__":main()
