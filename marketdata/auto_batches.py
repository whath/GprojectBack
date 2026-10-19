"""Recover per-instrument retry counts without losing a batch to a torn final log line."""
import json
import hashlib


def failed_attempts(path):
    if not path.exists():
        return {}
    raw=path.read_text(encoding='utf-8');lines=raw.splitlines();counts={};torn=False
    for index,line in enumerate(lines):
        try:row=json.loads(line)
        except json.JSONDecodeError:
            if index==len(lines)-1 and not raw.endswith('\n'):
                # Preserve the torn bytes separately, then atomically repair only this log's tail.
                digest=hashlib.sha256(line.encode()).hexdigest()[:16]
                path.with_name(path.name+'.interrupted-'+digest).write_text(line,encoding='utf-8')
                temporary=path.with_name(path.name+'.recovering')
                temporary.write_text('\n'.join(lines[:index])+('\n' if index else ''),encoding='utf-8')
                temporary.replace(path)
                torn=True
                continue
            raise ValueError('Automatic capture log is corrupt before its final line') from None
        if row['status'] not in ('complete','budget_deferred','not_listed_at_target'):
            counts[row['id']]=counts.get(row['id'],0)+1
    if raw and not raw.endswith('\n') and not torn:
        with path.open('a',encoding='utf-8') as stream:stream.write('\n')
    return counts
