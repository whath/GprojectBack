"""Build a credential-free release with a bounded initial collection universe."""
import hashlib
import json
import tarfile
from pathlib import Path


def main():
    root=Path(__file__).resolve().parents[1]
    output=root/"data"/"deploy"
    output.mkdir(parents=True,exist_ok=True)
    cfg=json.loads((root/"config/universe.json").read_text(encoding="utf-8"))
    cfg.update(history_days=60,cn_all_stocks=False,cn_all_etfs=False,cn_all_boards=False,
               cn_stocks=["000001","600519","300750","688981","920779"])
    config_path=output/"universe.json"
    config_path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    archive=output/"personal-market-data.tar.gz"
    paths=[root/p for p in ("Dockerfile","compose.yaml",".dockerignore",".env.example","requirements.txt","README.md")]
    paths+=sorted((root/"marketdata").glob("*.py"))
    paths+=sorted((root/"deploy").glob("*.sh"))
    paths+=sorted((root/"deploy").glob("*.conf"))
    paths += [root/"scripts/collect_acceptance.py",root/"scripts/smoke_api.py",root/"scripts/check_live_api.py",root/"scripts/check_hardening_api.py",root/"scripts/check_expanded_api.py",root/"scripts/pull_cloud_backup.py",root/"docs/BACKEND-EXPANSION-20260930.md"]
    manifest=[]
    with tarfile.open(archive,"w:gz") as tar:
        for path in paths+[config_path]:
            name="config/universe.json" if path==config_path else path.relative_to(root).as_posix()
            tar.add(path,arcname=name,recursive=False)
            manifest.append({"path":name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
    report={"archive":str(archive),"sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
            "initial_scope":"configured sample pool, 60 calendar days; no existing market data or credentials bundled",
            "files":manifest}
    (output/"manifest.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with tarfile.open(archive) as tar:
        names=tar.getnames()
        assert all(not Path(n).is_absolute() and ".." not in Path(n).parts for n in names)
        assert not any(n==".env" or n.startswith(("data/",".venv/",".git/")) for n in names)
        packed=json.load(tar.extractfile("config/universe.json"))
        assert packed["cn_all_stocks"] is False and len(packed["cn_stocks"])==5
    print(json.dumps({k:v for k,v in report.items() if k!="files"},ensure_ascii=False))


if __name__=="__main__":
    main()
