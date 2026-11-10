"""Versioned owner decisions. Recommendations never write or auto-accept music."""
import base64
import copy
import hashlib
import json

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from . import db, media, security

FIELDS = tuple(sorted(set(media._TEXT_FRAMES) | set(media._CUSTOM_FIELDS))) + ('cover', 'lrc')
PROVIDERS = ('musicbrainz', 'shazam', 'acoustid', 'coverartarchive', 'lrclib', 'local_cover', 'local_replaygain', 'theaudiodb', 'discogs')
SOURCE_KEY = 'field_source_priorities'


def value_hash(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def normalized_value(field, value):
    if field == 'cover':
        if not value:
            return ''
        if not isinstance(value, str) or not value.startswith('data:image/'):
            raise media.MediaError('invalid_cover', 'Обложка должна содержать изображение')
        try:
            return hashlib.sha256(base64.b64decode(value.split(',', 1)[1], validate=True)).hexdigest()
        except (ValueError, IndexError) as exc:
            raise media.MediaError('invalid_cover', 'Неверное изображение обложки') from exc
    if field == 'lrc':
        return value or ''
    return [str(item) for item in value] if isinstance(value, list) else [str(value)] if value is not None else []


def current_value(settings, row, field):
    meta = json.loads(row['metadata'])
    if field == 'cover':
        if not meta.get('has_art'):
            return ''
        image = media.read_art(settings.work_root / row['relative_path'], settings.roots)
        if not image:
            raise media.MediaError('cover_unavailable', 'Обложку не удалось прочитать; закрепление не выполнено')
        return hashlib.sha256(image[0]).hexdigest()
    if field == 'lrc':
        return meta.get('lrc') or meta.get('lyrics') or ''
    return normalized_value(field, meta.get('tags', {}).get(field))


def load(conn, track_id):
    row = conn.execute('SELECT value FROM settings WHERE key=?', ('track_field_policy:' + track_id,)).fetchone()
    return json.loads(row['value']) if row else {'version': 0, 'locks': {}}


def sources(conn):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (SOURCE_KEY,)).fetchone()
    result = json.loads(row['value']) if row else {'version': 0, 'priorities': {}}
    defaults = {}
    for field in FIELDS:
        first = ['coverartarchive', 'local_cover', 'theaudiodb'] if field == 'cover' else ['lrclib'] if field == 'lrc' else ['local_replaygain'] if field.startswith('replaygain_') else ['musicbrainz', 'shazam', 'discogs', 'theaudiodb']
        defaults[field] = list(dict.fromkeys(first + list(PROVIDERS)))
    return {**result, 'defaults': defaults, 'manual_first': True, 'automatic_apply': False, 'providers': list(PROVIDERS), 'fields': list(FIELDS)}


def stale_fields(settings, row, policy):
    stale = []
    for field, lock in policy['locks'].items():
        # An unchanged full revision already proves unchanged artwork bytes.
        if field == 'cover' and lock['input_sha256'] == row['sha256']:
            continue
        if value_hash(current_value(settings, row, field)) != lock['value_sha256']:
            stale.append(field)
    return stale


def payload_values(payload):
    result = dict(payload.get('tags', {}))
    for field in ('cover', 'lrc'):
        if field in payload:
            result[field] = payload[field]
    return result


def validate_payload(settings, conn, row, payload, mode='manual'):
    """Filter candidates, reject attempted writes; old proposals cannot bypass locks."""
    result = copy.deepcopy(payload)
    policy = load(conn, row['id'])
    stale = stale_fields(settings, row, policy)
    blocked = []
    for field, value in payload_values(result).items():
        lock = policy['locks'].get(field)
        if lock and value_hash(normalized_value(field, value)) != lock['value_sha256']:
            blocked.append(field)
            if mode == 'candidate':
                if field in {'cover', 'lrc'}:
                    result.pop(field, None)
                else:
                    result.get('tags', {}).pop(field, None)
                result.get('field_sources', {}).pop(field, None)
    if mode != 'candidate' and (blocked or mode == 'apply' and stale):
        raise media.MediaError('field_locked' if blocked else 'field_lock_stale', 'Закрепленные поля требуют решения владельца: ' + ', '.join(sorted(set(blocked + stale))))
    result['field_policy'] = {'version': policy['version'], 'blocked_fields': sorted(blocked), 'stale_fields': stale}
    from . import lyrics_document
    result = lyrics_document.check_payload(conn, row, result, mode)
    return result


def require_writable_fields(payload):
    if not (payload.get('tags') or payload.get('cover') or payload.get('lrc') is not None):
        raise media.MediaError('proposal_empty', 'В предложении нет тегов, обложки или текста для записи')


def assert_apply(settings, row, payload, conn=None):
    require_writable_fields(payload)
    if conn is not None:
        validate_payload(settings, conn, row, payload, 'apply')
    else:
        with db.connect(settings.db_path) as connection:
            validate_payload(settings, connection, row, payload, 'apply')


def require_payload(settings, conn, row, payload, mode='manual'):
    try:
        return validate_payload(settings, conn, row, payload, mode)
    except media.MediaError as exc:
        raise security.error(409, exc.code, exc.message) from exc


def selected_payload(settings, conn, row, chosen, retrieved_at):
    """Owner selects candidates; configurable priorities resolve each field explicitly."""
    config = sources(conn)
    available = {}
    source_rows = []
    for candidate in chosen:
        from . import album_gain_policy
        album_gain_policy.candidate(settings, conn, row, candidate)
        source = {key: candidate[key] for key in ('id', 'provider', 'source_url', 'source_path', 'identification', 'timing_verified', 'usage') if key in candidate}
        source_rows.append(source)
        values = dict(candidate.get('tags', {}))
        if candidate.get('lrc') or candidate.get('plain_lyrics'):
            values['lrc'] = candidate.get('lrc') or candidate['plain_lyrics']
        if candidate.get('cover'):
            values['cover'] = candidate['cover']
        for field, value in values.items():
            if field in FIELDS:
                available.setdefault(field, []).append((candidate, value))
    result = {'tags': {}, 'sources': source_rows, 'field_sources': {}, 'source_priority_version': config['version']}
    for field, choices in available.items():
        priority = config['priorities'].get(field, config['defaults'][field])
        candidate, value = min(choices, key=lambda item: (priority.index(item[0]['provider']) if item[0]['provider'] in priority else len(priority), len(item[0].get('match', {}).get('reasons', [])), item[0]['id']))
        if field == 'lrc' and candidate['provider'] == 'lrclib' and candidate.get('lrc'):
            cleaned = media.trim_candidate_lrc_end_marker(value, json.loads(row['metadata']).get('duration'))
            if cleaned != value:
                result['candidate_lyric_adjustment'] = 'empty_end_marker_removed'
                value = cleaned
        if field in {'cover', 'lrc'}:
            result[field] = value
        else:
            result['tags'][field] = value
        provenance = {'provider': candidate['provider'], 'candidate_id': candidate['id'], 'retrieved_at': retrieved_at,
            'response_sha256': value_hash(candidate), 'value_sha256': value_hash(normalized_value(field, value)),
            'reason': 'owner_selected_field_priority'}
        for key in ('source_url', 'source_path', 'usage', 'album_gain_group'):
            if key in candidate:
                provenance[key] = candidate[key]
        result['field_sources'][field] = provenance
    result = validate_payload(settings, conn, row, result, 'candidate')
    if 'lrc' not in result:
        result.pop('candidate_lyric_adjustment', None)
    return _keep_selected_sources(result)


def _keep_selected_sources(result):
    used_candidates = {source['candidate_id'] for source in result['field_sources'].values()}
    result['sources'] = [source for source in result['sources'] if source['id'] in used_candidates]
    return result


def exclude_selected_fields(payload, excluded):
    values = set(payload_values(payload))
    if not excluded < values:
        raise media.MediaError('invalid_selection_fields', 'Оставьте хотя бы одно поле из сравнения')
    for field in excluded:
        if field in {'cover', 'lrc'}:
            payload.pop(field)
        else:
            payload['tags'].pop(field)
        payload['field_sources'].pop(field, None)
    if 'lrc' in excluded:
        payload.pop('candidate_lyric_adjustment', None)
    return _keep_selected_sources(payload)


def cover_lyrics_draft_parts(payload):
    """Partition a saved mixed draft without losing field-specific provenance."""
    if set(payload_values(payload)) != {'cover', 'lrc'} or not payload.get('cover') or not payload.get('lrc'):
        raise media.MediaError('mixed_cover_draft_required', 'В черновике должны быть обложка и текст')
    origins = payload.get('field_sources', {})
    assigned = {source.get('candidate_id') for source in origins.values() if isinstance(source, dict)}
    selection = payload.get('slot_selection') or {}

    def part(field, extra, slot_key):
        value = {key: copy.deepcopy(payload[key]) for key in (field, *extra) if key in payload}
        value['tags'] = {}
        origin = origins.get(field)
        if origin:
            value['field_sources'] = {field: copy.deepcopy(origin)}
        candidate = origin.get('candidate_id') if isinstance(origin, dict) else None
        # A legacy source with no explicit field binding stays on both parts.
        value['sources'] = [copy.deepcopy(source) for source in payload.get('sources', [])
                            if source.get('id') == candidate or source.get('id') not in assigned]
        if selection.get(slot_key):
            value['slot_selection'] = {'inventory_sha256': selection['inventory_sha256'], slot_key: selection[slot_key]}
        return value

    return (part('cover', ('artwork_transform',), 'cover_slot_id'),
            part('lrc', ('lyrics_document',), 'lyrics_slot_id'))


class LocksInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fields: list[str] = Field(max_length=len(FIELDS))
    base_version: int = Field(ge=0)


class SourcesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    priorities: dict[str, list[str]] = Field(max_length=len(FIELDS))
    base_version: int = Field(ge=0)


def install(app, settings):
    @app.get('/api/tracks/{track_id}/policy')
    def get_policy(request: Request, track_id: str):
        security.require_session(request, settings)
        with db.connect(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row:
                raise security.error(404, 'track_not_found', 'Трек не найден')
            policy = load(conn, track_id)
            try:
                stale = stale_fields(settings, row, policy)
            except media.MediaError as exc:
                raise security.error(409, exc.code, exc.message) from exc
            return {**policy, 'track_id': track_id, 'input_sha256': row['sha256'], 'fields': list(FIELDS), 'stale_fields': stale}

    @app.put('/api/tracks/{track_id}/policy')
    def put_policy(request: Request, track_id: str, body: LocksInput):
        security.require_session(request, settings, True, owner_only=True)
        if set(body.fields) - set(FIELDS) or len(body.fields) != len(set(body.fields)):
            raise security.error(422, 'invalid_field_policy', 'Выберите поддерживаемые поля без повторов')
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row:
                raise security.error(404, 'track_not_found', 'Трек не найден')
            if request.headers.get('if-match', '').strip('"') != row['sha256']:
                raise security.error(412, 'revision_conflict', 'Файл изменился; обновите карточку')
            current = load(conn, track_id)
            if body.base_version != current['version']:
                raise security.error(412, 'policy_conflict', 'Закрепления изменились в другой сессии')
            busy = conn.execute("SELECT 1 FROM jobs j,json_each(j.payload,'$.items') i WHERE j.kind IN ('apply','rollback') AND j.state IN ('queued','running','paused','waiting_authorization','interrupted') AND json_extract(i.value,'$.track_id')=? LIMIT 1", (track_id,)).fetchone()
            if busy:
                raise security.error(409, 'track_mutation_pending', 'Дождитесь завершения записи или отмените ее перед изменением закреплений')
            locks = {}
            for field in body.fields:
                try:
                    locks[field] = current['locks'].get(field) or {'input_sha256': row['sha256'], 'value_sha256': value_hash(current_value(settings, row, field)), 'created_at': db.utcnow()}
                except media.MediaError as exc:
                    raise security.error(409, exc.code, exc.message) from exc
            result = {'version': current['version'] + 1, 'locks': locks, 'updated_at': db.utcnow()}
            try:
                stale = stale_fields(settings, row, result)
            except media.MediaError as exc:
                raise security.error(409, exc.code, exc.message) from exc
            conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('track_field_policy:' + track_id, db.encode(result)))
            db.event(conn, 'track_field_policy_changed', {'track_id': track_id, 'version': result['version'], 'fields': list(locks)})
            return {**result, 'track_id': track_id, 'input_sha256': row['sha256'], 'fields': list(FIELDS), 'stale_fields': stale}

    @app.get('/api/policies/sources')
    def get_sources(request: Request):
        security.require_session(request, settings, owner_only=True)
        with db.connect(settings.db_path) as conn:
            return sources(conn)

    @app.put('/api/policies/sources')
    def put_sources(request: Request, body: SourcesInput):
        security.require_session(request, settings, True, owner_only=True)
        if set(body.priorities) - set(FIELDS) or any(not values or len(values) != len(set(values)) or set(values) - set(PROVIDERS) for values in body.priorities.values()):
            raise security.error(422, 'invalid_source_priorities', 'Приоритеты содержат неподдерживаемые поля, источники или повторы')
        with db.transaction(settings.db_path) as conn:
            current = sources(conn)
            if body.base_version != current['version']:
                raise security.error(412, 'policy_conflict', 'Приоритеты изменились в другой сессии')
            result = {'version': current['version'] + 1, 'priorities': body.priorities, 'updated_at': db.utcnow()}
            conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (SOURCE_KEY, db.encode(result)))
            db.event(conn, 'source_priorities_changed', {'version': result['version']})
            return sources(conn)
