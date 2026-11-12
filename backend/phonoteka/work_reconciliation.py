"""Explicit full-byte reconciliation of a volatile Work device identifier.

This is not an inventory scan or an input-readiness observation. Only a fresh
owner job may rebind the same canonical directory/inode after every current
catalogue path has been accounted for. Native writes still keep their SHA CAS.
"""
import hashlib
import json
from pathlib import Path
import re

from . import automation, db, media

PARAM = 'reconcile_root'
CONTRACT = 'work-root-full-sha256-v1'
PROOF = 'work_root_reconciliation'


def _identity(settings):
    from .worker import root_identity
    return root_identity(settings.work_root)


def _snapshot(conn):
    return [dict(row) for row in conn.execute(
        'SELECT id,relative_path,sha256,status FROM tracks WHERE is_current=1 ORDER BY id')]


def _digest(snapshot):
    return hashlib.sha256(db.encode(snapshot).encode()).hexdigest()


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _path(settings, relative):
    part = Path(relative)
    if (not isinstance(relative, str) or not relative or part.is_absolute()
            or part.as_posix() != relative or '..' in part.parts or '\\' in relative):
        raise media.MediaError('reconciliation_unsafe_path', 'В каталоге есть неканонический путь')
    path = settings.work_root / part
    if path == settings.work_root or settings.work_root not in path.parents:
        raise media.MediaError('reconciliation_unsafe_path', 'Путь выходит за пределы рабочей папки')
    return path


def _grant(conn, worker, job):
    control = conn.execute(f'SELECT {automation.CONTROL_COLUMNS} FROM jobs WHERE id=?', (job['id'],)).fetchone()
    if worker.stopping or not control or control['cancel_requested']:
        raise media.MediaError('cancelled', 'Сверка рабочей папки отменена')
    reason = automation.grant_reason(conn, control)
    if reason:
        raise media.MediaError(reason, 'Разрешение на сверку рабочей папки изменилось')
    if control['auth_epoch'] != job['auth_epoch']:
        raise media.MediaError('authorization_epoch_changed', 'Права задачи изменились после начала сверки')
    if control['kind'] != 'scan' or control['auth_token_id'] or control['auth_limited']:
        raise media.MediaError('reconciliation_owner_required', 'Сверку запускает пользователь через интерфейс')
    recovery = conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()
    if recovery and recovery['value'] != 'false':
        raise media.MediaError('recovery_required', 'Сначала завершите восстановление файловых операций')


def _authorize(worker, job):
    worker.authorize_grant(job)
    with db.connect(worker.settings.db_path) as conn:
        _grant(conn, worker, job)


def _verify_one(settings, row, expected_root, authorize):
    """Return a full-file proof or an explicitly frozen known absence."""
    authorize()
    if _identity(settings) != expected_root:
        raise media.MediaError('root_identity_changed', 'Рабочая папка изменилась во время сверки')
    path = _path(settings, row['relative_path'])
    media._no_links(path, missing=True)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if row['status'] != 'missing':
            raise media.MediaError('reconciliation_missing_file', 'Ожидаемый файл отсутствует') from None
        if _identity(settings) != expected_root:
            raise media.MediaError('root_identity_changed', 'Рабочая папка изменилась во время сверки')
        authorize()
        return {'exists': False, 'stamp': None}
    if not isinstance(row['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
        raise media.MediaError('reconciliation_invalid_revision', 'В каталоге отсутствует полный SHA-256')
    media._validate(path, settings.roots)
    digest = hashlib.sha256()
    since_authorization = 0
    with media._file(path) as stream:
        import os
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise media.MediaError('file_changing', 'Файл изменился перед чтением')
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
            since_authorization += len(block)
            if since_authorization >= 64 * 1024 * 1024:
                authorize()
                since_authorization = 0
        after = os.fstat(stream.fileno())
    if _stamp(before) != _stamp(after) or _stamp(path.lstat()) != _stamp(after):
        raise media.MediaError('file_changing', 'Файл изменился во время полного чтения')
    if digest.hexdigest() != row['sha256']:
        raise media.MediaError('reconciliation_revision_mismatch', 'Содержимое файла отличается от каталога')
    if _identity(settings) != expected_root:
        raise media.MediaError('root_identity_changed', 'Рабочая папка изменилась во время сверки')
    authorize()
    return {'exists': True, 'stamp': _stamp(after)}


def _recheck(settings, snapshot, observations, expected_root, authorize):
    """Final metadata/absence checks in the short commit transaction; no hashes."""
    for row, observed in zip(snapshot, observations):
        authorize()
        if _identity(settings) != expected_root:
            raise media.MediaError('root_identity_changed', 'Рабочая папка изменилась после чтения')
        path = _path(settings, row['relative_path'])
        media._no_links(path, missing=True)
        try:
            current = path.lstat()
        except FileNotFoundError:
            if observed['exists']:
                raise media.MediaError('file_changing', 'Проверенный файл исчез до завершения сверки') from None
            continue
        if not observed['exists'] or _stamp(current) != observed['stamp']:
            raise media.MediaError('file_changing', 'Состояние файла изменилось после полного чтения')
        media._validate(path, settings.roots)


async def run(worker, job):
    settings = worker.settings
    payload = json.loads(job['payload'])
    params = payload.get('params')
    if (job['kind'] != 'scan' or not isinstance(params, dict) or set(params) != {PARAM} or params[PARAM] is not True or payload.get('items')
            or payload.get('auth_token_id') or payload.get('auth_limited')):
        raise media.MediaError('reconciliation_owner_required', 'Нужна отдельная новая задача сверки из интерфейса')
    await worker.io(_authorize, worker, job)
    current = await worker.io(_identity, settings)
    with db.transaction(settings.db_path) as conn:
        _grant(conn, worker, job)
        if conn.execute('SELECT 1 FROM job_items WHERE job_id=? LIMIT 1', (job['id'],)).fetchone():
            raise media.MediaError('reconciliation_fresh_job_required', 'Начните новую сверку без прежних результатов')
        fresh = conn.execute('SELECT completed,failed FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if fresh['completed'] or fresh['failed']:
            raise media.MediaError('reconciliation_fresh_job_required', 'Начните новую сверку без прежних результатов')
        saved = conn.execute("SELECT value FROM settings WHERE key='work_root_identity'").fetchone()
        if not saved:
            raise media.MediaError('root_identity_missing', 'Для первого подключения выполните обычное сканирование')
        previous_raw = saved['value']; previous = json.loads(previous_raw)
        if previous == current:
            conn.execute('UPDATE jobs SET total=0 WHERE id=?', (job['id'],))
            db.event(conn, 'work_root_reconciliation_not_needed', {'job_id': job['id'], 'hashes': 0})
            snapshot = None
        else:
            if (not isinstance(previous, dict) or set(previous) != {'device', 'inode', 'path'}
                    or type(previous.get('device')) is not int or type(previous.get('inode')) is not int
                    or previous.get('device') == current['device'] or previous.get('path') != current['path']
                    or previous.get('inode') != current['inode']):
                raise media.MediaError('root_identity_changed', 'Автоматическая сверка разрешена только при изменении номера устройства')
            snapshot = _snapshot(conn)
            if not snapshot:
                raise media.MediaError('reconciliation_empty_catalog', 'Пустой каталог не подтверждает рабочую папку')
            conn.execute('UPDATE jobs SET total=? WHERE id=?', (len(snapshot), job['id']))
    if snapshot is None:
        worker.finish(job['id'], 'succeeded')
        return {'state': 'already_current', 'hashes': 0}
    observations = []
    physical = set()
    for row in snapshot:
        try:
            worker.item_result(job['id'], row['id'], 'running', {'track_id': row['id']})
            worker.item_action(job, {'track_id': row['id']}, 'root_sha256')
            observation = await worker.io(_verify_one, settings, row, current, lambda: _authorize(worker, job))
            if observation['exists']:
                file_id = observation['stamp'][:2]
                if file_id in physical:
                    raise media.MediaError('reconciliation_path_alias', 'Несколько записей указывают на один физический файл')
                physical.add(file_id)
            observations.append(observation)
            result = {'track_id': row['id'], 'sha256': row['sha256'], 'verified_root': True, 'hashed': observation['exists']}
            if not observation['exists']:
                result.update(skipped=True, reason='known_missing_remains_absent')
            worker.item_result(job['id'], row['id'], 'succeeded', result)
        except Exception as exc:
            worker.item_result(job['id'], row['id'], 'failed', worker.failure(exc))
            raise
    if not physical:
        raise media.MediaError('reconciliation_no_anchors', 'Нет существующих файлов для подтверждения рабочей папки')
    with db.transaction(settings.db_path) as conn:
        _grant(conn, worker, job)
        latest = conn.execute("SELECT value FROM settings WHERE key='work_root_identity'").fetchone()
        if not latest or latest['value'] != previous_raw or _snapshot(conn) != snapshot:
            raise media.MediaError('reconciliation_catalog_changed', 'Каталог или привязка рабочей папки изменились')
        _recheck(settings, snapshot, observations, current, lambda: _grant(conn, worker, job))
        if _identity(settings) != current:
            raise media.MediaError('root_identity_changed', 'Рабочая папка изменилась перед фиксацией сверки')
        proof = {'contract': CONTRACT, 'job_id': job['id'], 'from': previous, 'to': current,
                 'manifest_sha256': _digest(snapshot), 'count': len(snapshot), 'anchors': len(physical),
                 'known_missing_absent': len(snapshot) - len(physical),
                 'bytes_verified': sum(o['stamp'][2] for o in observations if o['exists']), 'at': db.utcnow()}
        changed = conn.execute("UPDATE settings SET value=? WHERE key='work_root_identity' AND value=?", (db.encode(current), previous_raw))
        if changed.rowcount != 1:
            raise media.MediaError('reconciliation_catalog_changed', 'Привязка рабочей папки уже изменилась')
        conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (PROOF, db.encode(proof)))
        db.event(conn, 'work_root_reconciled', {'job_id': job['id'], 'manifest_sha256': proof['manifest_sha256'],
                                              'count': proof['count'], 'anchors': proof['anchors']})
    worker.finish(job['id'], 'succeeded')
    return proof
