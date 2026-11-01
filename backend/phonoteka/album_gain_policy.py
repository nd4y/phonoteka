"""Frozen Album Gain composition. Tag-only writes preserve the audio identity."""
import hashlib
import json

from fastapi import Request

from . import db, media, release_review, security

FIELDS = {'replaygain_album_gain', 'replaygain_album_peak'}


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def saved(conn, key):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else None


def trusted_identity(row):
    metadata = json.loads(row['metadata']); audit = metadata.get('audit') or {}
    if audit.get('state') == 'passed' and audit.get('sha256') == row['sha256']:
        return audit.get('audio_identity')
    # persist_mutation stores the independently verified packet identity in the
    # mutation history even when no older full audit can be reused.
    return None


def release_context(conn, review_id, version, ids):
    if review_id is None:
        if version is not None: raise media.MediaError('album_gain_release_required', 'Укажите ID принятой сверки релиза')
        return None
    value = saved(conn, 'release_review:' + review_id)
    if not value or value['version'] != version:
        raise media.MediaError('album_gain_release_stale', 'Сверка релиза изменилась')
    report = release_review.report(conn, value)
    if report['state'] not in {'complete', 'subset_complete'} or set(report.get('matched_track_ids', [])) != set(ids):
        raise media.MediaError('album_gain_release_stale', 'Состав не совпадает с принятым релизом/подмножеством')
    return {'id': review_id, 'version': version, 'release_id': value['release_id'],
            'decision_sha256': digest({'expected_slots': value.get('expected_slots'), 'note': value.get('note'),
                                      'version': value['version'], 'release_id': value['release_id']})}


def build(settings, items, params, authorize=lambda: None):
    members = []
    for item in items:
        authorize()
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],))
        if not row or row['sha256'] != item['sha256']:
            raise media.MediaError('album_gain_stale', 'Участник Album Gain изменился во время измерения')
        path = media._validate(settings.work_root / row['relative_path'], settings.roots)
        media._expect(path, row['sha256'])
        identity = trusted_identity(row) or media.audio_identity(path)
        media._expect(path, row['sha256'])
        members.append({'track_id': row['id'], 'relative_path': row['relative_path'],
                        'measured_file_revision': row['sha256'], 'audio_identity': identity})
    with db.connect(settings.db_path) as conn:
        release = release_context(conn, params.get('release_review_id'), params.get('release_review_version'), [item['track_id'] for item in items])
    authorize()
    value = {'contract': 'album-gain-group-v1', 'method': 'libebur128_joint_gating', 'reference_lufs': -18,
             'members': members, 'release': release, 'composition_mode': 'accepted_release' if release else 'owner_selected_files'}
    return {**value, 'id': digest(value), 'context_sha256': digest(value)}


def register(conn, context):
    key = 'album_gain_group:' + context['id']
    previous = saved(conn, key)
    if previous and previous != context:
        raise media.MediaError('album_gain_context_conflict', 'Контекст измерения уже принадлежит другой группе')
    conn.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key, db.encode(context)))
    for member in context['members']:
        row = conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1', (member['track_id'],)).fetchone()
        if not row or row['sha256'] != member['measured_file_revision']:
            raise media.MediaError('album_gain_stale', 'Участник изменился перед фиксацией Album Gain')
        conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('album_gain_membership:' + member['track_id'], db.encode(context['id'])))


def validate(settings, conn, reference, track_id, *, verify_files=False):
    if not isinstance(reference, dict):
        raise media.MediaError('album_gain_context_required', 'Пересчитайте Album Gain: нет замороженного состава')
    context = saved(conn, 'album_gain_group:' + str(reference.get('id')))
    if not context or context.get('context_sha256') != reference.get('context_sha256'):
        raise media.MediaError('album_gain_stale', 'Контекст Album Gain недоступен или изменился')
    if digest({key: value for key, value in context.items() if key not in {'id', 'context_sha256'}}) != context['context_sha256']:
        raise media.MediaError('album_gain_stale', 'Нарушена целостность контекста Album Gain')
    if track_id not in {member['track_id'] for member in context['members']}:
        raise media.MediaError('album_gain_wrong_member', 'Этот файл не участвовал в измерении Album Gain')
    members = []
    for member in context['members']:
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (member['track_id'],)).fetchone()
        if not row or row['status'] in {'missing', 'error'} or saved(conn, 'album_gain_membership:' + member['track_id']) != context['id']:
            raise media.MediaError('album_gain_stale', 'Изменился принятый состав Album Gain')
        identity = trusted_identity(row)
        if row['sha256'] != member['measured_file_revision'] and identity != member['audio_identity']:
            known = conn.execute("SELECT details FROM history WHERE track_id=? AND after_sha256=? AND kind IN ('apply','rollback') ORDER BY created_at DESC,rowid DESC LIMIT 1", (row['id'], row['sha256'])).fetchone()
            identity = json.loads(known['details']).get('audio_identity') if known else None
            if identity != member['audio_identity']:
                raise media.MediaError('album_gain_stale', 'Аудиосодержимое участника Album Gain изменилось')
        if row['relative_path'] != member['relative_path']:
            raise media.MediaError('album_gain_stale', 'Изменилось сопоставление участника Album Gain')
        members.append(row)
    if context.get('release'):
        release = context['release']
        current = release_context(conn, release['id'], release['version'], [member['track_id'] for member in context['members']])
        if current != release: raise media.MediaError('album_gain_release_stale', 'Решение по релизу/подмножеству изменилось')
    if verify_files:
        for row in members:
            media._expect(media._validate(settings.work_root / row['relative_path'], settings.roots), row['sha256'])
    return context


def candidate(settings, conn, row, value):
    if value.get('provider') == 'local_replaygain' and (str(value.get('id', '')).startswith('album-replaygain:') or FIELDS & value.get('tags', {}).keys()):
        validate(settings, conn, value.get('album_gain_group'), row['id'])


def payload(settings, conn, row, value, *, verify_files=False):
    for field in FIELDS & value.get('tags', {}).keys():
        source = value.get('field_sources', {}).get(field, {})
        reference = source.get('album_gain_group')
        if reference or source.get('provider') == 'local_replaygain' and str(source.get('candidate_id', '')).startswith('album-replaygain:'):
            validate(settings, conn, reference, row['id'], verify_files=verify_files)


def depends_on_group(value):
    """Only mutations carrying measured Album Gain need the shared commit lane."""
    for field in FIELDS & value.get('tags', {}).keys():
        source = value.get('field_sources', {}).get(field, {})
        if source.get('album_gain_group') or source.get('provider') == 'local_replaygain' and str(source.get('candidate_id', '')).startswith('album-replaygain:'):
            return True
    return False


def install(app, settings):
    @app.get('/api/album-gain-groups/{identity}')
    def detail(request: Request, identity: str):
        security.require_session(request, settings)
        with db.connect(settings.db_path) as conn:
            value = saved(conn, 'album_gain_group:' + identity)
            if not value: raise security.error(404, 'album_gain_group_missing', 'Группа измерения не найдена')
            try:
                validate(settings, conn, {'id': identity, 'context_sha256': value['context_sha256']}, value['members'][0]['track_id'])
                state, reason = 'current', None
            except media.MediaError as exc:
                state, reason = 'stale', exc.code
        return {**value, 'state': state, 'reason': reason}
