"""Explicit, two-phase cleanup of registered state artifacts; keep-all by default.

No music/Ready deletion and no automatic scheduling. The CLI operator must stop
the worker, inspect a fully verified preview and explicitly confirm its plan hash.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import db, media, security

DEFAULT = {'version': 0, 'keep_all': True, 'min_age_days': 7, 'keep_last': 1}
TERMINAL = {'succeeded', 'failed', 'partial', 'cancelled', 'cancelled_partial', 'needs_review', 'conflict'}


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def saved(conn, key, default):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def policy(conn):
    return saved(conn, 'retention_policy', DEFAULT.copy())


def pins(conn):
    return saved(conn, 'retention_pins', {'version': 0, 'items': {}})


def stamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return time.time()  # An unknown age must never become eligible.


def safe_artifact(settings, relative, *, missing=False):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative or not path.parts or path.parts[0] not in {'backups', 'exports'}:
        raise ValueError('Artifact escaped the registered state roots')
    target = settings.state_dir / path
    media._no_links(target, missing=missing)
    if not target.resolve().is_relative_to(settings.state_dir.resolve()):
        raise ValueError('Artifact escaped state')
    return target


def file_signature(path, verify):
    media._no_links(path)
    stat = path.stat()
    if not path.is_file() or stat.st_nlink != 1:
        raise ValueError('Only ordinary, unlinked artifact files can be pruned')
    value = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'inode': stat.st_ino, 'device': stat.st_dev}
    if verify:
        value['sha256'] = media.sha256_file(path)
    return value


def references(conn, now):
    jobs = [dict(row) for row in conn.execute('SELECT id,state,payload FROM jobs ORDER BY id') if row['state'] not in TERMINAL]
    leases = [json.loads(row['value']) for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'export_lease:%' ORDER BY key")]
    leases = [lease for lease in leases if lease['expires_at'] > now]
    intents = []
    for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'publication_intent:%' ORDER BY key"):
        value = json.loads(row['value'])
        if not conn.execute("SELECT 1 FROM history WHERE track_id=? AND json_extract(details,'$.publication_after.job_id')=? LIMIT 1", (value['track_id'], value['after']['job_id'])).fetchone():
            intents.append(value)
    latest = conn.execute("SELECT value FROM settings WHERE key='latest_export'").fetchone()
    maintenance=[json.loads(row['value']) for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'maintenance_artifact_refs:%' ORDER BY key")]
    maintenance=[reference for reference in maintenance if not reference.get('released')]
    return {'jobs': jobs, 'leases': leases, 'publication_intents': intents,
            'maintenance_artifact_refs':maintenance,
            'latest_export': latest['value'] if latest else None,
            'recovery_required': (conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone() or {'value': 'false'})['value'] == 'true'}


def pending_journals(settings):
    root = settings.state_dir / 'retention'
    if not root.exists():
        return []
    media._no_links(root)
    result = []
    for path in root.glob('*.json'):
        media._no_links(path)
        try:
            value = json.loads(path.read_text())
            if value.get('state') not in {'committed', 'reconciled'}:
                result.append(path.name)
        except (OSError, ValueError):
            result.append(path.name)
    return sorted(result)


def preview(settings, *, verify_files=False, now=None):
    now = time.time() if now is None else now
    with db.connect(settings.db_path) as conn:
        configured = policy(conn)
        pinned = pins(conn)
        refs = references(conn, now)
        histories = [dict(row) for row in conn.execute("SELECT h.*,t.relative_path AS track_path FROM history h LEFT JOIN tracks t ON t.id=h.track_id WHERE h.kind IN ('apply','rollback','publish','rollback_ready') ORDER BY h.created_at DESC,h.rowid DESC")]
    objects = []
    for row in histories:
        details = json.loads(row['details'])
        if not details.get('backup_path') or details.get('backup_pruned'):
            continue
        reasons = []
        files = {}
        try:
            backup = Path(details['backup_path'])
            relative = backup.relative_to(settings.state_dir).as_posix()
            target = safe_artifact(settings, relative)
            if not relative.startswith('backups/' + row['track_id'] + '/'):
                raise ValueError('Backup belongs to another track')
            files[relative] = file_signature(target, verify_files)
            journal = details.get('journal_path')
            if journal:
                journal_relative = Path(journal).relative_to(settings.state_dir).as_posix()
                journal_path = safe_artifact(settings, journal_relative)
                document = json.loads(journal_path.read_text())
                if document.get('state') != 'committed' or document.get('backup') != str(backup):
                    reasons.append('unfinished_or_mismatched_journal')
                files[journal_relative] = file_signature(journal_path, verify_files)
            else:
                reasons.append('journal_missing')
            if verify_files and files[relative]['sha256'] != row['before_sha256']:
                reasons.append('backup_integrity_mismatch')
        except (OSError, ValueError, media.MediaError):
            reasons.append('artifact_missing_or_unsafe')
        objects.append({'id': 'history:' + row['id'], 'kind': 'history', 'identity': row['id'], 'track_id': row['track_id'],
                        'label': row['track_path'] or row['track_id'],
                        'family': 'ready' if row['kind'] in {'publish', 'rollback_ready'} else 'work',
                        'created_at': row['created_at'], 'files': files, 'protected_reasons': reasons,
                        'primary_path': relative if files else None, 'expected_backup_sha256': row['before_sha256']})
    exports_root = settings.state_dir / 'exports'
    if exports_root.exists():
        media._no_links(exports_root)
        for directory in sorted(exports_root.iterdir()):
            try:
                if str(uuid.UUID(directory.name)) != directory.name:
                    continue
                media._no_links(directory)
                if not directory.is_dir():
                    continue
                manifest_path = directory / 'manifest.json'
                media._no_links(manifest_path)
                manifest = json.loads(manifest_path.read_text())
                if manifest.get('id') != directory.name or not manifest.get('snapshot_available'):
                    continue  # Unknown directories are never cleanup candidates.
                files = {}
                for path in directory.rglob('*'):
                    media._no_links(path)
                    if path.is_file():
                        files[path.relative_to(settings.state_dir).as_posix()] = file_signature(path, verify_files)
                    elif not path.is_dir():
                        raise ValueError('Special export artifact')
                reasons = []
                if verify_files:
                    for entry in manifest['files']:
                        relative = f"exports/{directory.name}/files/{entry['path']}"
                        if relative not in files or files[relative]['sha256'] != entry['sha256']:
                            reasons.append('export_integrity_mismatch')
                objects.append({'id': 'export:' + directory.name, 'kind': 'export', 'identity': directory.name,
                                'label': 'Immutable export ' + manifest['created_at'],
                                'family': 'export', 'created_at': manifest['created_at'], 'files': files, 'protected_reasons': reasons,
                                'primary_path': f'exports/{directory.name}/manifest.json'})
            except (OSError, ValueError, KeyError, media.MediaError):
                continue  # Unregistered/damaged data is retained for diagnosis.
    groups = {}
    for item in objects:
        group = (item.get('track_id'), item['family'])
        groups.setdefault(group, []).append(item)
    for group in groups.values():
        group.sort(key=lambda item: stamp(item['created_at']), reverse=True)
        for item in group[:configured['keep_last']]:
            item['protected_reasons'].append('keep_last')
        if verify_files and group[0]['kind'] == 'history':
            readable = next((item for item in group if item['files'] and all(reason == 'keep_last' for reason in item['protected_reasons'])), None)
            if readable:
                readable['protected_reasons'].append('last_readable_rollback')
    active_tracks = set()
    history_refs = set()
    for queued in refs['jobs']:
        payload = json.loads(queued['payload'])
        for item in payload.get('items', []):
            if item.get('track_id'):
                active_tracks.add(item['track_id'])
            if item.get('history_id'):
                history_refs.add(item['history_id'])
        if payload.get('params', {}).get('history_id'):
            history_refs.add(payload['params']['history_id'])
    intent_tracks = {intent['track_id'] for intent in refs['publication_intents']}
    unresolved = pending_journals(settings)
    for item in objects:
        reasons = item['protected_reasons']
        if configured['keep_all']:
            reasons.append('keep_all_policy')
        age = max(stamp(item['created_at']), max((info['mtime_ns'] / 1e9 for info in item['files'].values()), default=now))
        if age > now - configured['min_age_days'] * 86400:
            reasons.append('minimum_age')
        if item['id'] in pinned['items']:
            reasons.append('owner_pin')
        if any(item['id'] in reference.get('artifact_ids',[]) for reference in refs['maintenance_artifact_refs']):
            reasons.append('maintenance_checkpoint_reference')
        if refs['recovery_required']:
            reasons.append('recovery_required')
        if unresolved:
            reasons.append('unfinished_retention_journal')
        if item['kind'] == 'history':
            if item['track_id'] in active_tracks or item['identity'] in history_refs:
                reasons.append('active_job_reference')
            if item['track_id'] in intent_tracks:
                reasons.append('unreconciled_publication_intent')
        else:
            if item['identity'] == refs['latest_export']:
                reasons.append('latest_export')
            if any(lease['export_id'] == item['identity'] for lease in refs['leases']):
                reasons.append('active_export_lease')
        item['protected_reasons'] = list(dict.fromkeys(reasons))
        item['eligible'] = not reasons and bool(item['files'])
        item['bytes'] = sum(info['size'] for info in item['files'].values())
    created = time.time()
    plan = {'schema': 1, 'created_at': created, 'expires_at': created + 900, 'fully_verified': verify_files,
            'policy': configured, 'pins_version': pinned['version'], 'references_revision': digest(refs),
            'objects': objects, 'unreconciled_journals': unresolved, 'music_deletion': False, 'ready_deletion': False,
            'eligible_bytes': sum(item['bytes'] for item in objects if item['eligible']),
            'eligible_count': sum(item['eligible'] for item in objects), 'cli_operator_confirmation_required': True}
    return {**plan, 'plan_id': digest(plan)}


@contextmanager
def export_lock(settings, identity, *, exclusive=False):
    import fcntl
    uuid.UUID(identity)
    root = settings.state_dir / 'export-locks'
    media._make_directory(root)
    path = root / (identity + '.lock')
    media._no_links(path, missing=True)
    with path.open('a+b') as handle:
        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield handle


def _mark_pruned(conn, item, plan_id, *, recovered=False):
    if item['kind'] == 'history':
        row = conn.execute('SELECT details FROM history WHERE id=?', (item['identity'],)).fetchone()
        details = json.loads(row['details'])
        details.update(backup_pruned=True, backup_pruned_at=db.utcnow(), backup_pruned_plan=plan_id,
                       backup_path=None, journal_path=None, retention_recovered=recovered)
        conn.execute('UPDATE history SET details=? WHERE id=?', (db.encode(details), item['identity']))
    else:
        conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('pruned_export:' + item['identity'], db.encode({'at': db.utcnow(), 'plan_id': plan_id})))


def apply(settings, plan, approval_token):
    from .worker import scheduler_guard, verify_storage
    if not plan.get('fully_verified') or digest({key: value for key, value in plan.items() if key != 'plan_id'}) != plan.get('plan_id') or approval_token != plan['plan_id']:
        raise ValueError('A fully verified, unmodified plan and explicit CLI operator confirmation are required')
    if time.time() > plan['expires_at']:
        raise ValueError('Preview expired; inspect a fresh preview')
    if not settings.writable:
        raise ValueError('Verified maintenance storage configuration is required')
    verify_storage(settings)
    removed, skipped = [], []
    journal_root = settings.state_dir / 'retention'
    media._make_directory(journal_root)
    journal_path = journal_root / (plan['plan_id'] + '.json')
    journal = {'state': 'prepared', 'plan': plan, 'attempted': [], 'removed': []}
    with scheduler_guard(settings.state_dir), media.writer_guard(settings.storage_guard_dir, settings.instance_id):
        if pending_journals(settings):
            raise ValueError('Reconcile the previous retention journal before cleanup')
        with db.transaction(settings.db_path) as conn:
            if policy(conn) != plan['policy'] or pins(conn)['version'] != plan['pins_version'] or digest(references(conn, time.time())) != plan['references_revision']:
                raise ValueError('Policy, pins, leases or job references changed; inspect a new preview')
            # BEGIN IMMEDIATE coordinates every new owner pin, delivery lease and
            # queued history reference until filesystem effects and metadata agree.
            current = {item['id']: item for item in preview(settings, verify_files=False)['objects']}
            for item in plan['objects']:
                if not item['eligible']:
                    continue
                if item['id'] not in current or not current[item['id']]['eligible']:
                    raise ValueError('Artifact acquired protection after preview')
                if set(current[item['id']]['files']) != set(item['files']):
                    raise ValueError('Artifact membership changed after preview')
                for relative, expected in item['files'].items():
                    if file_signature(safe_artifact(settings, relative), True) != expected:
                        raise ValueError('Artifact changed after preview; nothing from this plan is removed')
            media._journal(journal_path, journal)
            try:
                for item in plan['objects']:
                    if not item['eligible']:
                        continue
                    guard = export_lock(settings, item['identity'], exclusive=True) if item['kind'] == 'export' else None
                    try:
                        if guard:
                            guard.__enter__()
                    except BlockingIOError:
                        skipped.append({'id': item['id'], 'reason': 'active_download_file_lock'})
                        continue
                    try:
                        journal['attempted'].append(item['id']); media._journal(journal_path, journal)
                        if item['kind'] == 'export':
                            root = settings.state_dir / 'exports'
                            target = safe_artifact(settings, 'exports/' + item['identity'])
                            if target.resolve().parent != root.resolve() or not shutil.rmtree.avoids_symlink_attacks:
                                raise ValueError('Safe recursive deletion is unavailable')
                            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                            try:
                                shutil.rmtree(item['identity'], dir_fd=descriptor)
                            finally:
                                os.close(descriptor)
                        else:
                            for relative in item['files']:
                                target = safe_artifact(settings, relative)
                                descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                                try:
                                    os.unlink(target.name, dir_fd=descriptor)
                                finally:
                                    os.close(descriptor)
                        removed.append(item['id']); journal['removed'] = removed; media._journal(journal_path, journal)
                        _mark_pruned(conn, item, plan['plan_id'])
                    finally:
                        if guard:
                            guard.__exit__(None, None, None)
                db.event(conn, 'retention_applied', {'plan_id': plan['plan_id'], 'removed': len(removed), 'skipped': len(skipped)})
            except BaseException:
                journal['state'] = 'interrupted'; media._journal(journal_path, journal)
                raise
        journal['state'] = 'committed'; media._journal(journal_path, journal)
    return {'plan_id': plan['plan_id'], 'removed': removed, 'skipped': skipped, 'music_deleted': False, 'ready_deleted': False}


def reconcile(settings):
    from .worker import scheduler_guard, verify_storage
    verify_storage(settings)
    if not settings.writable:
        raise ValueError('Verified maintenance storage configuration is required')
    result = []
    with scheduler_guard(settings.state_dir), media.writer_guard(settings.storage_guard_dir, settings.instance_id):
        for name in pending_journals(settings):
            path = settings.state_dir / 'retention' / name
            media._no_links(path)
            journal = json.loads(path.read_text())
            plan = journal['plan']
            if digest({key: value for key, value in plan.items() if key != 'plan_id'}) != plan['plan_id']:
                raise ValueError('Retention journal plan is inconsistent')
            conflicts = []
            with db.transaction(settings.db_path) as conn:
                for item in plan['objects']:
                    if item['id'] not in journal['attempted']:
                        continue
                    remaining = [relative for relative in item['files'] if safe_artifact(settings, relative, missing=True).exists()]
                    primary_missing = item.get('primary_path') and not safe_artifact(settings, item['primary_path'], missing=True).exists()
                    if remaining and not primary_missing:
                        conflicts.append(item['id'])
                    else:
                        _mark_pruned(conn, item, plan['plan_id'], recovered=True)
                db.event(conn, 'retention_reconciled', {'plan_id': plan['plan_id'], 'conflicts': len(conflicts)})
            if not conflicts:
                journal['state'] = 'reconciled'; media._journal(path, journal)
            result.append({'plan_id': plan['plan_id'], 'conflicts': conflicts, 'state': journal['state']})
    return {'journals': result, 'filesystem_deletions': 0}


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    keep_all: bool
    min_age_days: int = Field(ge=1, le=36500)
    keep_last: int = Field(ge=1, le=10000)


class PinInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    kind: str = Field(pattern='^(history|export)$')
    identity: str = Field(min_length=1, max_length=36)
    pinned: bool
    reason: str = Field(default='', max_length=500)


def install(app, settings):
    @app.get('/api/retention')
    def get_retention(request: Request):
        security.require_session(request, settings, owner_only=True)
        with db.connect(settings.db_path) as conn:
            leases = [json.loads(row['value']) for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'export_lease:%'")]
            leases = [{key: lease[key] for key in ('id','export_id','purpose','created_at','expires_at')} for lease in leases if lease['expires_at'] > time.time()]
            return {'policy': policy(conn), 'pins': pins(conn), 'active_export_leases': leases, 'delivery_lease_ttl_seconds': 1800,
                    'apply_route': 'CLI operator only', 'automatic_cleanup': False}

    @app.get('/api/retention/preview')
    def get_preview(request: Request, page: int = Query(default=1,ge=1), page_size: int = Query(default=20,ge=1,le=100)):
        security.require_session(request, settings, owner_only=True)
        try:
            result = preview(settings, verify_files=False)
        except (OSError, ValueError, media.MediaError):
            raise security.error(409,'retention_preview_unavailable','Артефакты недоступны или требуют ручной сверки') from None
        return {**result, 'objects': result['objects'][(page-1)*page_size:page*page_size], 'total_objects': len(result['objects']),
                'page': page, 'page_size': page_size}

    @app.put('/api/retention/policy')
    def put_policy(request: Request, body: PolicyInput):
        security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            previous = policy(conn)
            if previous['version'] != body.base_version:
                raise security.error(412, 'retention_policy_changed', 'Политика хранения изменилась')
            result = body.model_dump(exclude={'base_version'}) | {'version': previous['version'] + 1}
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('retention_policy', db.encode(result)))
            db.event(conn, 'retention_policy_changed', {'version': result['version'], 'keep_all': result['keep_all']})
            return result

    @app.put('/api/retention/pins')
    def put_pin(request: Request, body: PinInput):
        security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            result = pins(conn)
            if result['version'] != body.base_version:
                raise security.error(412, 'retention_pins_changed', 'Закрепления изменились')
            identity = body.kind + ':' + body.identity
            if body.pinned:
                if len(body.reason.strip()) < 3:
                    raise security.error(422, 'pin_reason_required', 'Укажите причину закрепления')
                if body.kind == 'history':
                    row = conn.execute('SELECT details FROM history WHERE id=?', (body.identity,)).fetchone()
                    data = json.loads(row['details']) if row else {}
                    if not data.get('backup_path') or not Path(data['backup_path']).is_file():
                        raise security.error(409, 'backup_unavailable', 'Резервная версия недоступна')
                else:
                    try:
                        uuid.UUID(body.identity)
                        path = safe_artifact(settings, 'exports/' + body.identity + '/manifest.json')
                        if not path.is_file():
                            raise ValueError('missing')
                    except (ValueError, OSError, media.MediaError):
                        raise security.error(409, 'export_unavailable', 'Экспорт недоступен') from None
                result['items'][identity] = {'reason': body.reason.strip(), 'created_at': db.utcnow()}
            else:
                result['items'].pop(identity, None)
            result['version'] += 1
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('retention_pins', db.encode(result)))
            db.event(conn, 'retention_pin_changed', {'kind': body.kind, 'identity': body.identity, 'pinned': body.pinned})
            return result


def main():
    from .config import Settings
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    draft = sub.add_parser('preview'); draft.add_argument('--output', type=Path, required=True)
    execute = sub.add_parser('apply'); execute.add_argument('--plan', type=Path, required=True); execute.add_argument('--approval-token', required=True)
    sub.add_parser('reconcile')
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == 'preview':
        value = preview(settings, verify_files=True)
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        result = {'plan_file': str(args.output), 'plan_id': value['plan_id'], 'eligible_bytes': value['eligible_bytes'],
                  'eligible_count': value['eligible_count'], 'instruction': 'Inspect the entire report before explicit apply; plan hash is confirmation, not authorization'}
    elif args.command == 'apply':
        result = apply(settings, json.loads(args.plan.read_text()), args.approval_token)
    else:
        result = reconcile(settings)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
