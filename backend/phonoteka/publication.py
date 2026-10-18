"""Revision-specific Ready plans and journal reconciliation, separate from work."""
import json
from pathlib import Path

from fastapi import Query, Request

from . import db, intake, lyrics_review, media, provider_policy, security


def ready_target(settings, relative, *, write=False, missing=False):
    path = Path(relative)
    if path.is_absolute() or not path.parts or '..' in path.parts or '\\' in relative or '\n' in relative or '\r' in relative:
        raise media.MediaError('unsafe_path', 'Недопустимый относительный путь Ready')
    return media._validate(settings.publication_root / 'Ready' / path, settings.roots, write=write, missing=missing)


def freeze_target(row):
    metadata = json.loads(row['metadata'])
    previous = metadata.get('publication')
    return {'contract': 'ready-target-v1', 'track_id': row['id'], 'work_revision': row['sha256'],
            'ready_path': intake.publication_relative(row['relative_path'], metadata),
            'expected_publication': previous, 'expected_ready_sha256': previous.get('sha256') if previous else None}


def assert_target(settings, row, frozen):
    if not isinstance(frozen, dict) or frozen != freeze_target(row):
        raise media.MediaError('conflict', 'Состояние Ready изменилось после утверждения плана публикации')
    target = ready_target(settings, frozen['ready_path'], write=True, missing=True)
    expected = frozen['expected_ready_sha256']
    if expected is None:
        if target.exists():
            raise media.MediaError('conflict', 'В Ready уже существует файл без утвержденной исходной ревизии')
    elif not target.is_file() or media.sha256_file(target) != expected:
        raise media.MediaError('conflict', 'Файл Ready изменился после утверждения плана')
    return frozen


def intent_key(job_id, track_id):
    return 'publication_intent:' + job_id + ':' + track_id


def write_intent(settings, job, item, after, *, audit=None):
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],))
    if not row or row['sha256'] != item['sha256']:
        raise media.MediaError('conflict', 'Рабочая ревизия изменилась до операции Ready')
    before = json.loads(row['metadata']).get('publication')
    value = {'track_id': row['id'], 'work_revision': item['sha256'], 'relative_path': after['relative_path'],
             'before': before, 'after': after, 'audit': audit, 'kind': job['kind']}
    key = intent_key(job['id'], row['id'])
    with db.transaction(settings.db_path) as conn:
        previous = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if previous:
            existing = json.loads(previous['value'])
            if existing != value:
                raise media.MediaError('conflict', 'План Ready уже зафиксирован для другой ревизии')
        else:
            conn.execute('INSERT INTO settings VALUES(?,?)', (key, db.encode(value)))
    return value


def publish_intent(settings, job, item, row, gate, audit):
    meta = json.loads(row['metadata'])
    after = {'sha256': item['sha256'], 'relative_path': intake.publication_relative(item['relative_path'], meta),
             'published_at': db.utcnow(), 'job_id': job['id'], 'profile': gate['profile'],
             'review_version': gate['review_version'], 'document_revision': gate['document_revision'], 'lyrics_gate': gate,
             'exceptions': gate['exceptions'], 'exception': gate['exception_reason'],
             'usage_provenance': meta.get('usage_provenance', {})}
    return write_intent(settings, job, item, after, audit=audit)


def load_intent(settings, job_id, track_id):
    value = db.setting(settings.db_path, intent_key(job_id, track_id))
    if not value:
        raise media.MediaError('publication_intent_missing', 'Нет зафиксированного плана Ready: требуется ручная сверка журнала')
    return json.loads(value)


def persist(settings, job, item, result):
    """Record an already committed Ready effect; never inspect/rewrite work tags."""
    intent = load_intent(settings, job['id'], item['track_id'])
    target = ready_target(settings, intent['relative_path'])
    if result['sha256'] != intent['after']['sha256']:
        raise media.MediaError('conflict', 'Журнал Ready не совпадает с зафиксированным планом')
    media._expect(target, result['sha256'])
    media._expect(media._validate(settings.work_root / item['relative_path'], settings.roots), intent['work_revision'])
    after = {**intent['after'], 'operation_id': result['operation_id']}
    details = {**result, 'publication_before': intent['before'], 'publication_after': after,
               'publication_path': intent['relative_path'], 'work_revision': intent['work_revision'], 'work_unchanged': True}
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],)).fetchone()
        if not row or row['sha256'] != intent['work_revision']:
            raise media.MediaError('conflict', 'Рабочая ревизия изменилась при фиксации Ready')
        meta = json.loads(row['metadata'])
        if meta.get('publication') != intent['before']:
            known = conn.execute('SELECT id FROM history WHERE id=?', (result['operation_id'],)).fetchone()
            if known:
                return
            raise media.MediaError('conflict', 'Каталог Ready изменился после утверждения плана')
        meta['publication'] = after
        if job['kind'] == 'publish' and intent.get('audit'):
            meta['audit'] = intent['audit']
            if intent['audit'].get('hash_verified_at'):
                meta['hash_verified_at'] = intent['audit']['hash_verified_at']
        conn.execute('UPDATE tracks SET metadata=?,updated_at=? WHERE id=? AND sha256=?',
                     (db.encode(meta), db.utcnow(), row['id'], intent['work_revision']))
        conn.execute('INSERT OR IGNORE INTO history VALUES(?,?,?,?,?,?,?,?)',
                     (result['operation_id'], row['id'], job['id'], job['kind'], result.get('before_sha256'),
                      result['sha256'], db.encode(details), db.utcnow()))
        db.event(conn, 'ready_committed', {'track_id': row['id'], 'job_id': job['id'], 'kind': job['kind'],
                                         'ready_sha256': result['sha256'], 'work_sha256': intent['work_revision']})


def rollback_selection(conn, row, history_id, expected_ready_sha):
    pub = json.loads(row['metadata']).get('publication')
    if not pub or pub.get('sha256') != expected_ready_sha:
        raise media.MediaError('conflict', 'Ревизия Ready изменилась. Обновите план')
    history = conn.execute("SELECT * FROM history WHERE id=? AND track_id=? AND kind IN ('publish','rollback_ready')", (history_id, row['id'])).fetchone()
    if not history:
        raise media.MediaError('ready_rollback_unavailable', 'Версия публикации не найдена')
    details = json.loads(history['details'])
    before = details.get('publication_before')
    if not details.get('backup_path') or not history['before_sha256'] or not before:
        raise media.MediaError('ready_rollback_unavailable', 'До этой публикации файла Ready не было; удаление не выполняется')
    if before.get('sha256') != history['before_sha256'] or details.get('publication_path') != pub.get('relative_path'):
        raise media.MediaError('conflict', 'Резервная версия относится к другому пути Ready')
    return {'history_id': history_id, 'expected_ready_sha256': expected_ready_sha}


def rollback(settings, job, item, authorize):
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],))
    if not row or row['sha256'] != item['sha256']:
        raise media.MediaError('conflict', 'Рабочая ревизия изменилась до отката Ready')
    media._expect(media._validate(settings.work_root / item['relative_path'], settings.roots), item['sha256'])
    with db.connect(settings.db_path) as conn:
        rollback_selection(conn, row, item['history_id'], item['expected_ready_sha256'])
        history = conn.execute('SELECT * FROM history WHERE id=?', (item['history_id'],)).fetchone()
    details = json.loads(history['details'])
    before = details['publication_before']
    with db.connect(settings.db_path) as conn:
        provider_policy.require_sources(conn, provider_policy.references({'field_sources': before.get('usage_provenance', {})}), 'export')
    pub = json.loads(row['metadata'])['publication']
    target = ready_target(settings, pub['relative_path'], write=True)
    backup = Path(details['backup_path'])
    media._no_links(backup)
    if not backup.is_absolute() or not backup.resolve().is_relative_to((settings.state_dir / 'backups' / row['id']).resolve()):
        raise media.MediaError('invalid_backup', 'Резервная версия находится вне истории этого файла')
    journal = backup.with_name(backup.name.split('.', 1)[0] + '.json')
    media._no_links(journal)
    document = json.loads(journal.read_text())
    if document.get('backup') != str(backup) or document.get('target') != str(target) or document.get('before_sha256') != history['before_sha256']:
        raise media.MediaError('invalid_backup', 'Журнал не подтверждает резервную версию Ready')
    after = {**before, 'job_id': job['id'], 'restored_at': db.utcnow(), 'restored_from_history': history['id']}
    write_intent(settings, job, item, after)
    def authorize_ready():
        authorize()
        with db.connect(settings.db_path) as conn:
            provider_policy.require_sources(conn, provider_policy.references({'field_sources': before.get('usage_provenance', {})}), 'export')
    result = media.rollback(target, backup, item['expected_ready_sha256'], settings.roots,
                            backup_sha256=history['before_sha256'], backup_dir=settings.state_dir / 'backups' / row['id'] / job['id'],
                            authorize=authorize_ready)
    persist(settings, job, item, result)
    return {key: value for key, value in result.items() if key not in {'backup_path', 'journal_path'}} | {'work_unchanged': True}


def plan_item(settings, row, *, verify=False):
    from . import publication_sidecars
    meta = json.loads(row['metadata'])
    pub = meta.get('publication')
    relative = pub['relative_path'] if pub else intake.publication_relative(row['relative_path'], meta)
    gate = lyrics_review.publication_gate(settings, row) if row['is_current'] else {'state': 'blocked', 'reasons': ['track_no_longer_current']}
    target = settings.publication_root / 'Ready' / relative
    actual = None
    integrity = 'not_checked'
    exists = False
    try:
        target = ready_target(settings, relative, missing=True)
        exists = target.is_file()
        if verify and exists:
            actual = media.sha256_file(target)
            integrity = 'matches' if pub and actual == pub['sha256'] else 'untracked_destination' if not pub else 'conflict'
        elif pub and not exists:
            integrity = 'missing'
    except (media.MediaError, OSError):
        integrity = 'unsafe_or_unavailable'
    stale = bool(pub) and (pub['sha256'] != row['sha256'] or not row['is_current'])
    action = 'add' if not pub else 'stale_ready' if stale else 'unchanged'
    if pub and pub['sha256'] != row['sha256'] and row['is_current']:
        action = 'replace'
    if integrity in {'conflict', 'missing', 'unsafe_or_unavailable', 'untracked_destination'} or (exists and not pub):
        action = 'conflict'
    versions = []
    for history in db.fetchall(settings.db_path, "SELECT id,kind,before_sha256,after_sha256,details,created_at FROM history WHERE track_id=? AND kind IN ('publish','rollback_ready') ORDER BY created_at DESC,rowid DESC LIMIT 30", (row['id'],)):
        data = json.loads(history['details'])
        baseline = data.get('publication_before')
        backup = Path(data['backup_path']) if data.get('backup_path') else None
        retained = bool(backup and backup.is_file() and not backup.is_symlink() and not data.get('backup_pruned'))
        versions.append({'history_id': history['id'], 'kind': history['kind'], 'created_at': history['created_at'],
                         'ready_revision': history['after_sha256'], 'restore_revision': history['before_sha256'],
                         'profile': (data.get('publication_after') or {}).get('profile', 'legacy_unknown'),
                         'backup_pruned': bool(data.get('backup_pruned')), 'backup_retained': retained,
                         'rollback_available': bool(pub and baseline and retained and data.get('publication_path') == pub.get('relative_path'))})
    return {'track_id': row['id'], 'relative_path': row['relative_path'], 'ready_path': relative,
            'work_revision': row['sha256'], 'ready_revision': pub.get('sha256') if pub else None,
            'publication': pub, 'current_track': bool(row['is_current']), 'stale_ready': stale, 'action': action,
            'destination_exists': exists, 'destination_integrity': integrity, 'actual_ready_revision': actual,
            'gate': gate, 'versions': versions, 'sidecars': publication_sidecars.track_state(settings, row),
            'automatic_deletion': False, 'phone_changes': False}


def install(app, settings):
    @app.get('/api/publication/plan')
    def plan(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=30, ge=1, le=100),
             stale_only: bool = False):
        security.require_session(request, settings, owner_only=True)
        where = "json_extract(metadata,'$.publication.sha256') IS NOT NULL AND (is_current=0 OR json_extract(metadata,'$.publication.sha256')<>sha256)" if stale_only else "(is_current=1 OR json_extract(metadata,'$.publication.sha256') IS NOT NULL)"
        total = db.fetchone(settings.db_path, 'SELECT COUNT(*) AS count FROM tracks WHERE ' + where)['count']
        rows = db.fetchall(settings.db_path, 'SELECT * FROM tracks WHERE ' + where + ' ORDER BY relative_path,id LIMIT ? OFFSET ?', (page_size, (page - 1) * page_size))
        return {'items': [plan_item(settings, row) for row in rows], 'page': page, 'page_size': page_size, 'total': total,
                'automatic_deletion': False, 'phone_changes': False, 'integrity_verified': False}

    @app.get('/api/tracks/{track_id}/publication')
    def track_plan(request: Request, track_id: str, verify: bool = False):
        security.require_session(request, settings, owner_only=True)
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
        if not row:
            raise security.error(404, 'track_not_found', 'Трек не найден')
        return plan_item(settings, row, verify=verify)
