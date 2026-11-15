"""Cheap inventory reuse. A stat match is never a new hash or stability proof."""
from datetime import datetime
import json
import time

from . import db, input_readiness, media


def signature(info):
    return {name: getattr(info, 'st_' + name) for name in
            ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}


def reusable(settings, row, *, require_stable=True):
    try:
        meta = json.loads(row['metadata'])
        if (row['status'] in {'error', 'missing', 'superseded'} or meta.get('input_error')
                or meta.get('format') == 'cue' or meta.get('sha256') != row['sha256']
                or require_stable and input_readiness.describe(meta, grace_seconds=settings.input_grace_seconds)['state'] != 'ready'):
            return None
        verified = datetime.fromisoformat(meta.get('hash_verified_at', ''))
        if verified.tzinfo is None or verified.timestamp() > time.time() + 1:
            return None
        verified_ns = int(verified.timestamp() * 1_000_000_000)
        path = media._validate(settings.work_root / row['relative_path'], settings.roots)
        info = path.stat()
        current = signature(info)
        if (info.st_size != meta.get('size') or info.st_mtime_ns != meta.get('mtime_ns')
                or info.st_ctime_ns > verified_ns):
            return None
        previous = meta.get('inventory_stat')
        if previous is not None:
            if previous != current:
                return None
        else:
            # Legacy inventories have a full hash/date but no inode signature.
            # A replaced ancestor can hide an older file's own ctime.
            for parent in path.parents:
                if parent == settings.work_root.resolve():
                    break
                if parent.stat().st_ctime_ns > verified_ns:
                    return None
        return {'track_id': row['id'], 'sha256': row['sha256'],
                'relative_path': row['relative_path'], 'stat': current}
    except (OSError, ValueError, TypeError, AttributeError, media.MediaError):
        return None


def plan(settings, paths, done):
    with db.connect(settings.db_path) as conn:
        rows = {r['relative_path']: dict(r) for r in conn.execute('SELECT * FROM tracks WHERE is_current=1')}
    pending, skipped, finished = [], {}, set()
    for relative in paths:
        row = rows.get(relative)
        if relative in done:
            # A restart joins this scan's actual full reads, not a second pass.
            # Re-read a completed path if its file/row changed during downtime.
            item = done[relative]
            result = json.loads(item['result'])
            if (item['state'] != 'succeeded' or row and row['sha256'] == result.get('sha256')
                    and reusable(settings, row, require_stable=False)):
                finished.add(relative)
            pending.append(relative)
            continue
        reuse = reusable(settings, row) if row else None
        if reuse:
            skipped[relative] = reuse
        else:
            pending.append(relative)
    return pending, skipped, finished


def recheck(settings, skipped):
    """Catch an edit while other files were being inspected, before accepting skips."""
    with db.connect(settings.db_path) as conn:
        rows = {r['id']: dict(r) for r in conn.execute('SELECT * FROM tracks WHERE is_current=1')}
    valid, changed = {}, []
    for relative, before in skipped.items():
        row = rows.get(before['track_id'])
        current = reusable(settings, row) if row and row['sha256'] == before['sha256'] else None
        if current == before:
            valid[relative] = before
        else:
            changed.append(relative)
    return valid, changed


def public(value):
    if (not isinstance(value, dict) or value.get('mode') not in ('incremental', 'full')
            or any(type(value.get(k)) is not int or value[k] < 0 for k in ('discovered', 'unchanged'))
            or value['unchanged'] > value['discovered']):
        return None
    return {key: value[key] for key in ('mode', 'discovered', 'unchanged')}
