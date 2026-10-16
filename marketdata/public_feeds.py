"""Bounded anonymous publisher reads and atomic snapshots with last-good retention."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import HTTPException

from . import db


class PublicSource:
    def __init__(self, raw_dir=None):
        self.raw_dir = Path(raw_dir) if raw_dir else None

    def get(self, url, params=None):
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, params=params, timeout=(8, 25), stream=True,
                             headers={'User-Agent': 'PersonalEOD/0.6'}) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise ValueError('source response exceeds bounded size')
                    chunks.append(chunk)
                raw = b''.join(chunks)
                encoding = response.encoding or 'utf-8'
                if encoding.lower() == 'iso-8859-1':
                    encoding = 'utf-8'
                text = raw.decode(encoding)
                if self.raw_dir:
                    self.raw_dir.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256(raw).hexdigest()
                    key = hashlib.sha256(response.url.encode()).hexdigest()[:16]
                    (self.raw_dir / (key + '.raw')).write_bytes(raw)
                    (self.raw_dir / (key + '.json')).write_text(json.dumps(dict(
                        url=response.url, sha256=digest, bytes=len(raw), encoding=encoding,
                        collected_at=db.now_iso()), ensure_ascii=False), encoding='utf-8')
                return text

    def json(self, url, params=None):
        return json.loads(self.get(url, params))


def store(key, payload):
    if not payload.get('items') or not payload.get('source'):
        raise ValueError('nonempty source-qualified snapshot required')
    stamp = db.now_iso()
    payload = dict(payload, collected_at=stamp)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT payload FROM feed_snapshots WHERE feed_key=?', (key,)).fetchone()
        if old and json.loads(old[0]).get('source') != payload['source']:
            raise ValueError('source change requires a separate feed identity')
        con.execute("INSERT OR REPLACE INTO feed_snapshots VALUES(?,?,?,'complete',NULL)", (key, encoded, stamp))
    db.feed_attempt(key, 'complete')
    return len(payload['items'])


def load(key, *, now=None):
    with db.connect() as con:
        row = con.execute('SELECT * FROM feed_snapshots WHERE feed_key=?', (key,)).fetchone()
        attempt = con.execute('SELECT * FROM feed_attempts WHERE feed_key=?', (key,)).fetchone()
    if not row:
        raise HTTPException(503, 'feed not yet collected')
    try:
        def reject_constant(value):
            raise ValueError('nonfinite JSON constant')
        payload = json.loads(row['payload'],parse_constant=reject_constant)
        if row['status'] != 'complete' or not payload['items'] or not payload['source']:
            raise ValueError('invalid stored feed')
        stamp = datetime.fromisoformat(payload['collected_at'])
        now = now or datetime.now(timezone.utc)
        if stamp.tzinfo is None or stamp > now:
            raise ValueError('invalid collection clock')
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503, 'stored feed failed validation') from None
    return dict(payload, fetch_stale=(now - stamp).total_seconds() > 86400,
                last_attempt_status=attempt['status'] if attempt else None,
                last_attempt_at=attempt['last_attempt'] if attempt else None)
