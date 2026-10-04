"""Stable pagination leases for clients using the existing limit/offset protocol."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from . import db


def page(key, offset, limit, load, identity="", refresh=False, snapshot_id=None):
    base_key = hashlib.sha256(json.dumps([identity, key], sort_keys=True).encode()).hexdigest()
    def version_key(identifier):
        return hashlib.sha256((base_key+":"+identifier).encode()).hexdigest()
    cache_key = version_key(snapshot_id) if snapshot_id else base_key
    if snapshot_id and refresh:
        raise HTTPException(422, "snapshot_id and refresh cannot be combined")
    now = datetime.now(timezone.utc)
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        saved = con.execute("SELECT * FROM query_snapshots WHERE cache_key=?", (cache_key,)).fetchone()
        if saved is None and (offset or snapshot_id):
            raise HTTPException(409, "pagination snapshot unavailable; restart at offset=0")
        expired = saved is not None and datetime.fromisoformat(saved["expires_at"]) <= now
        if expired and (offset or snapshot_id):
            raise HTTPException(409, "pagination snapshot expired; restart at offset=0")
        if saved is None or expired or (refresh and offset == 0):
            payload = load(con)
            payload["snapshot_id"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            saved_payload = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            con.execute("DELETE FROM query_snapshots WHERE expires_at<?", ((now-timedelta(days=1)).isoformat(),))
            con.execute("INSERT OR REPLACE INTO query_snapshots VALUES(?,?,?)",
                        (cache_key, saved_payload, (now + timedelta(minutes=30)).isoformat()))
            con.execute("INSERT OR REPLACE INTO query_snapshots VALUES(?,?,?)",
                        (version_key(payload["snapshot_id"]),saved_payload,(now+timedelta(minutes=30)).isoformat()))
        else:
            payload = json.loads(saved["payload"])
        # Active scans retain their version; expiry is based on inactivity.
        con.execute("UPDATE query_snapshots SET expires_at=? WHERE cache_key=?",
                    ((now + timedelta(minutes=30)).isoformat(), cache_key))
        con.execute("INSERT OR IGNORE INTO query_snapshots VALUES(?,?,?)",
                    (version_key(payload["snapshot_id"]),json.dumps(payload,ensure_ascii=False,allow_nan=False),
                     (now+timedelta(minutes=30)).isoformat()))
        con.execute("UPDATE query_snapshots SET expires_at=? WHERE cache_key=?",
                    ((now+timedelta(minutes=30)).isoformat(),version_key(payload["snapshot_id"])))
    all_items = payload["items"]
    return dict(payload, items=all_items[offset:offset + limit])
