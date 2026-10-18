"""Atomic proposals for an explicit album group. Audio writes stay individual."""
import base64
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import uuid
from typing import Literal

from fastapi import Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import db, field_policy, media, provider_policy, release_review, security

MAX_FILES = 200
MAX_PLAN_BYTES = 256 * 1024 ** 2
TAG_FIELDS = set(field_policy.FIELDS) - {'cover', 'lrc'}
RELEASE_FIELDS = {'album', 'albumartist', 'date', 'musicbrainz_albumid'}


class CoverSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    track_id: str = Field(min_length=1, max_length=36)
    candidate_id: str = Field(min_length=1, max_length=200)
    input_sha256: str = Field(pattern='^[0-9a-f]{64}$')


class ExceptionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    exclude: bool = False
    reason: str = Field(default='Исключено владельцем', max_length=1000)
    skip_fields: list[str] = Field(default_factory=list, max_length=len(field_policy.FIELDS))
    tags: dict[str, str] = Field(default_factory=dict, max_length=len(TAG_FIELDS))


class BatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    track_ids: list[str] = Field(min_length=1, max_length=MAX_FILES)
    revisions: dict[str, str] = Field(max_length=MAX_FILES)
    base_proposals: dict[str, str | None] = Field(max_length=MAX_FILES)
    tags: dict[str, str] = Field(default_factory=dict, max_length=len(TAG_FIELDS))
    cover: str | None = Field(default=None, max_length=14 * 1024 ** 2)
    cover_source: CoverSource | None = None
    exceptions: dict[str, ExceptionInput] = Field(default_factory=dict, max_length=MAX_FILES)
    lock_mode: Literal['skip', 'reject'] = 'skip'
    release_review_id: str | None = Field(default=None, max_length=36)
    release_review_version: int | None = Field(default=None, ge=1)
    release_fields: list[str] = Field(default_factory=list, max_length=len(RELEASE_FIELDS))
    preview_token: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')


def check_tags(tags):
    if set(tags) - TAG_FIELDS or any(len(value) > 4096 or '\x00' in value for value in tags.values()):
        raise security.error(422, 'invalid_album_tags', 'Неподдерживаемые поля или значения тегов')


def check_cover(value):
    if not re.match(r'^data:image/(jpeg|png);base64,', value or ''):
        raise security.error(422, 'invalid_cover', 'Обложка должна быть JPEG или PNG')
    try:
        raw = base64.b64decode(value.split(',', 1)[1], validate=True)
    except (ValueError, IndexError) as exc:
        raise security.error(422, 'invalid_cover', 'Неверный формат обложки') from exc
    if len(raw) > 10 * 1024 ** 2:
        raise security.error(413, 'cover_too_large', 'Обложка превышает 10 МиБ')
    try:
        mime, _, _ = media._cover(raw)
    except media.MediaError as exc:
        raise security.error(422, exc.code, exc.message) from exc
    if mime != value.split(';', 1)[0][5:]:
        raise security.error(422, 'cover_mime_mismatch', 'Тип изображения не совпадает с содержимым')
    return value


def latest(conn, identity):
    return conn.execute('SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (identity,)).fetchone()


def row_context(conn, ids):
    items = []
    for identity in ids:
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (identity,)).fetchone()
        if not row:
            raise security.error(404, 'track_not_found', 'Один из выбранных файлов недоступен')
        meta = json.loads(row['metadata']); base = latest(conn, identity)
        analysis = conn.execute('SELECT payload,input_sha256,updated_at FROM analyses WHERE track_id=?', (identity,)).fetchone()
        candidates = []
        if analysis and analysis['input_sha256'] == row['sha256']:
            for candidate in json.loads(analysis['payload']).get('candidates', []):
                if candidate.get('kind') != 'cover':
                    continue
                acquired_at = datetime.fromisoformat(analysis['updated_at'].replace('Z', '+00:00')).timestamp()
                usage = candidate.get('usage') or provider_policy.candidate_usage(conn, candidate, acquired_at)
                candidates.append({'track_id': identity, 'input_sha256': row['sha256'], 'candidate_id': candidate['id'], 'provider': candidate['provider'],
                    'available': bool(candidate.get('cover')) and provider_policy._allowed(conn, usage, 'cache'), 'source_url': candidate.get('source_url', '')})
        items.append({'id': identity, 'relative_path': row['relative_path'], 'sha256': row['sha256'], 'tags': meta.get('tags', {}),
            'has_art': bool(meta.get('has_art')), 'base_proposal_id': base['id'] if base else None, 'locks': list(field_policy.load(conn, identity)['locks']),
            'cover_candidates': candidates, 'existing_proposal_fields': list(field_policy.payload_values(json.loads(base['payload']))) if base and base['input_sha256'] == row['sha256'] else [],
            'write_supported': bool(meta.get('write_supported')) and not meta.get('cue_album_image') and meta.get('format') != 'cue' and not meta.get('input_error') and row['status'] != 'missing'})
    return items


def release_context(conn, identity, version, ids):
    value = release_review.stored(conn, identity)
    result = release_review.report(conn, value)
    if result['stale'] or value['version'] != version:
        raise security.error(412, 'release_review_stale', 'Выбранный состав релиза изменился')
    if result['state'] not in {'complete', 'subset_complete'} or not value.get('reference'):
        raise security.error(409, 'release_review_unaccepted', 'Нужен однозначно сопоставленный полный релиз или ожидаемое подмножество')
    if not set(ids) <= set(result['matched_track_ids']):
        raise security.error(409, 'release_members_differ', 'Выбранные файлы не входят в принятый состав релиза')
    ref = value['reference']
    return value, {'album': ref['title'], 'albumartist': ref['artist'], 'date': ref['date'], 'musicbrainz_albumid': ref['id']}


def request_check(body):
    ids = body.track_ids
    if len(ids) != len(set(ids)) or set(body.revisions) != set(ids) or set(body.base_proposals) != set(ids) or set(body.exceptions) - set(ids):
        raise security.error(422, 'invalid_batch_members', 'Нужны уникальные ID, SHA и версия предложения каждого выбранного файла')
    if any(not re.fullmatch('[0-9a-f]{64}', value) for value in body.revisions.values()):
        raise security.error(422, 'invalid_revision', 'Нужен полный SHA-256 каждого файла')
    check_tags(body.tags)
    if set(body.release_fields) - RELEASE_FIELDS or len(body.release_fields) != len(set(body.release_fields)) or set(body.release_fields) & set(body.tags):
        raise security.error(422, 'invalid_release_fields', 'Поле должно иметь один явно выбранный источник')
    if bool(body.release_review_id) != bool(body.release_review_version) or body.release_fields and not body.release_review_id:
        raise security.error(422, 'release_review_required', 'Для полей релиза нужен ID и версия принятой сверки')
    if body.cover is not None and body.cover_source is not None:
        raise security.error(422, 'cover_source_conflict', 'Выберите загрузку обложки либо найденный источник')
    if body.cover is not None:
        check_cover(body.cover)
    for value in body.exceptions.values():
        check_tags(value.tags)
        if set(value.skip_fields) - (TAG_FIELDS | {'cover'}) or len(value.skip_fields) != len(set(value.skip_fields)) or set(value.tags) & set(value.skip_fields):
            raise security.error(422, 'invalid_batch_exception', 'Исключения содержат неподдерживаемые поля')


def source_cover(conn, body):
    if body.cover_source is None:
        return body.cover, None
    choice = body.cover_source
    row = conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1', (choice.track_id,)).fetchone()
    analysis = conn.execute('SELECT * FROM analyses WHERE track_id=?', (choice.track_id,)).fetchone()
    if not row or row['sha256'] != choice.input_sha256 or not analysis or analysis['input_sha256'] != choice.input_sha256:
        raise security.error(412, 'cover_source_stale', 'Источник обложки изменился; повторите выбор')
    candidate = next((value for value in json.loads(analysis['payload']).get('candidates', []) if value.get('id') == choice.candidate_id and value.get('kind') == 'cover'), None)
    if not candidate or not candidate.get('cover'):
        raise security.error(409, 'cover_source_unavailable', 'Обложка недоступна либо истекло разрешенное хранение')
    value = check_cover(candidate['cover'])
    acquired_at = datetime.fromisoformat(analysis['updated_at'].replace('Z', '+00:00')).timestamp()
    usage = candidate.get('usage') or provider_policy.candidate_usage(conn, candidate, acquired_at)
    provider_policy.require_sources(conn, [usage], 'cache')
    return value, {'provider': candidate['provider'], 'candidate_id': candidate['id'], 'retrieved_at': analysis['updated_at'],
        'source_url': candidate.get('source_url', ''), 'source_path': candidate.get('source_path', ''), 'usage': usage,
        'reason': 'owner_selected_album_cover', 'value_sha256': field_policy.value_hash(field_policy.normalized_value('cover', value))}


def build(settings, conn, body):
    request_check(body)
    included = [identity for identity in body.track_ids if not body.exceptions.get(identity, ExceptionInput()).exclude]
    if not included:
        raise security.error(422, 'empty_album_batch', 'Все файлы исключены из пакета')
    ref, release_values = release_context(conn, body.release_review_id, body.release_review_version, included) if body.release_review_id else (None, {})
    cover, cover_source = source_cover(conn, body)
    plans, payload_bytes, versions = [], 0, []
    for identity in included:
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (identity,)).fetchone()
        if not row or row['sha256'] != body.revisions[identity]:
            raise security.error(412, 'batch_revision_conflict', 'Версия одного из выбранных файлов изменилась; пакет не сохранен', track_id=identity)
        base = latest(conn, identity)
        if (base['id'] if base else None) != body.base_proposals[identity]:
            raise security.error(412, 'batch_proposal_conflict', 'В одной из карточек появилось другое предложение; пакет не сохранен', track_id=identity)
        meta = json.loads(row['metadata']); exception = body.exceptions.get(identity, ExceptionInput())
        plan = {'track_id': identity, 'input_sha256': row['sha256'], 'relative_path': row['relative_path'], 'base_proposal_id': base['id'] if base else None, 'skipped_fields': [], 'changes': [], 'error': None}
        try:
            if meta.get('input_error') or meta.get('format') == 'cue' or meta.get('cue_album_image') or not meta.get('write_supported') or row['status'] == 'missing':
                raise media.MediaError('batch_read_only', 'Для файла нет разрешенного профиля записи')
            shared = {'tags': dict(body.tags), 'field_sources': {}}
            for field in body.release_fields:
                if not release_values[field]:
                    raise media.MediaError('release_value_missing', 'В эталоне нет выбранного значения: ' + field)
                shared['tags'][field] = release_values[field]
                shared['field_sources'][field] = {'provider': 'musicbrainz', 'candidate_id': 'release:' + ref['release_id'], 'usage': ref['usage'],
                    'source_url': ref['reference']['source_url'], 'reason': 'owner_selected_album_release', 'value_sha256': field_policy.value_hash(field_policy.normalized_value(field, release_values[field]))}
            if cover is not None:
                shared['cover'] = cover
                if cover_source:
                    shared['field_sources']['cover'] = cover_source
            for field in exception.skip_fields:
                shared.get('tags', {}).pop(field, None)
                shared.pop(field, None)
                shared['field_sources'].pop(field, None)
            shared = field_policy.validate_payload(settings, conn, row, shared, 'candidate' if body.lock_mode == 'skip' else 'manual')
            plan['skipped_fields'] = sorted(set(exception.skip_fields + shared['field_policy']['blocked_fields']))
            # Explicit per-file replacement of a lock is rejected, never silently
            # converted into a general album preference.
            override = field_policy.validate_payload(settings, conn, row, {'tags': exception.tags}, 'manual')
            payload = deepcopy(json.loads(base['payload'])) if base and base['input_sha256'] == row['sha256'] else {'tags': {}}
            payload.setdefault('tags', {}).update(shared['tags'])
            payload['tags'].update(override['tags'])
            payload.setdefault('field_sources', {}).update(shared.get('field_sources', {}))
            if 'cover' in shared:
                payload['cover'] = shared['cover']
            for field in exception.tags:
                if field in payload['field_sources']:
                    payload['field_sources'][field]['owner_edited'] = True
                    payload['field_sources'][field]['value_sha256'] = field_policy.value_hash(field_policy.normalized_value(field, exception.tags[field]))
            if cover_source and 'cover' in shared:
                source = {'id': cover_source['candidate_id'], **{key: cover_source[key] for key in ('provider', 'source_url', 'source_path', 'usage')}}
                if source not in payload.setdefault('sources', []):
                    payload['sources'].append(source)
            payload = field_policy.validate_payload(settings, conn, row, payload, 'apply')
            payload = provider_policy.require_payload(conn, row, payload, 'cache')
            group_fields = {**shared.get('tags', {}), **override['tags'], **({'cover': shared['cover']} if 'cover' in shared else {})}
            for field, value in group_fields.items():
                before = field_policy.current_value(settings, row, field)
                after = field_policy.normalized_value(field, value)
                if before != after:
                    plan['changes'].append({'field': field, 'before': before, 'after': after})
            payload['album_batch_context'] = {'release_review_id': body.release_review_id, 'release_review_version': body.release_review_version, 'skipped_fields': plan['skipped_fields']}
            for source in provider_policy.references(payload):
                if source.get('provider'):
                    versions.append((source['provider'], source.get('data_type', 'metadata'), provider_policy.rule(conn, source['provider'], source.get('data_type', 'metadata'))['policy_version']))
            payload_bytes += len(db.encode(payload).encode())
            if payload_bytes > MAX_PLAN_BYTES:
                raise security.error(413, 'album_batch_too_large', 'Предложения превышают 256 МиБ; уменьшите обложку или разделите пакет')
            plan['payload'] = payload
        except media.MediaError as exc:
            plan['error'] = {'code': exc.code, 'message': exc.message}
        plans.append(plan)
    token = field_policy.value_hash({'request': body.model_dump(exclude={'preview_token'}), 'plans': plans, 'current_policies': sorted(set(versions))})
    excluded = [{'track_id': identity, 'reason': value.reason} for identity, value in body.exceptions.items() if value.exclude]
    return {'preview_token': token, 'plans': plans, 'excluded': excluded, 'saveable': all(not plan['error'] for plan in plans) and any(plan['changes'] for plan in plans), 'proposal_bytes': payload_bytes}


def public_preview(value):
    return {**value, 'plans': [{key: item for key, item in plan.items() if key != 'payload'} for plan in value['plans']]}


def install(app, settings):
    @app.get('/api/album-batches/context')
    def context(request: Request, ids: str = Query(max_length=7500), review_id: str | None = None, review_version: int | None = None):
        security.require_session(request, settings, owner_only=True)
        values = list(dict.fromkeys(ids.split(',')))
        if not 1 <= len(values) <= MAX_FILES:
            raise security.error(422, 'invalid_batch_members', 'Выберите от 1 до 200 файлов')
        with db.connect(settings.db_path) as conn:
            items = row_context(conn, values)
            ref, tags = release_context(conn, review_id, review_version, values) if review_id else (None, {})
            return {'items': items, 'release_tags': tags, 'release_review_id': ref['id'] if ref else None, 'release_review_version': ref['version'] if ref else None}

    @app.get('/api/album-batches/cover')
    def cover_preview(request: Request, track_id: str = Query(min_length=1, max_length=36), candidate_id: str = Query(min_length=1, max_length=200), input_sha256: str = Query(pattern='^[0-9a-f]{64}$')):
        security.require_session(request, settings, owner_only=True)
        try:
            body = BatchInput(track_ids=[track_id], revisions={track_id: input_sha256}, base_proposals={track_id: None},
                cover_source=CoverSource(track_id=track_id, candidate_id=candidate_id, input_sha256=input_sha256))
            with db.connect(settings.db_path) as conn:
                value, _ = source_cover(conn, body)
            return Response(base64.b64decode(value.split(',', 1)[1]), media_type=value.split(';', 1)[0][5:])
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc

    @app.post('/api/album-batches/preview')
    def preview(request: Request, body: BatchInput):
        security.require_session(request, settings, True, owner_only=True)
        try:
            with db.connect(settings.db_path) as conn:
                conn.execute('BEGIN')
                return public_preview(build(settings, conn, body))
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc

    @app.post('/api/album-batches/save', status_code=201)
    def save(request: Request, body: BatchInput):
        security.require_session(request, settings, True, owner_only=True)
        key = request.headers.get('idempotency-key', '')
        if not key or len(key) > 128 or not body.preview_token:
            raise security.error(428, 'batch_preview_required', 'Нужны preview_token и Idempotency-Key')
        key_hash = hashlib.sha256(key.encode()).hexdigest(); request_hash = field_policy.value_hash(body.model_dump())
        try:
            with db.transaction(settings.db_path) as conn:
                previous = conn.execute('SELECT value FROM settings WHERE key=?', ('album_batch_idempotency:' + key_hash,)).fetchone()
                if previous:
                    known = json.loads(previous['value'])
                    if known['request_hash'] != request_hash:
                        raise security.error(409, 'idempotency_conflict', 'Ключ уже использован для другого пакета')
                    return json.loads(conn.execute('SELECT value FROM settings WHERE key=?', ('album_batch:' + known['batch_id'],)).fetchone()['value'])
                plan = build(settings, conn, body)
                if plan['preview_token'] != body.preview_token:
                    raise security.error(412, 'batch_preview_stale', 'Предложения, закрепления или права изменились; повторите предпросмотр')
                if not plan['saveable']:
                    raise security.error(409, 'album_batch_blocked', 'В пакете есть ошибки либо нет изменений; предложения не сохранены')
                identity = str(uuid.uuid4()); now = db.utcnow(); entries = []
                for item in plan['plans']:
                    if not item['changes']:
                        entries.append({'track_id': item['track_id'], 'input_sha256': item['input_sha256'], 'state': 'unchanged', 'proposal_id': item['base_proposal_id'], 'skipped_fields': item['skipped_fields']})
                        continue
                    proposal_id = str(uuid.uuid4()); payload = item['payload']; payload['album_batch_context']['batch_id'] = identity
                    conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (proposal_id, item['track_id'], item['input_sha256'], db.encode(payload), now))
                    conn.execute("UPDATE tracks SET status='needs_review',updated_at=? WHERE id=?", (now, item['track_id']))
                    db.event(conn, 'proposal_created', {'track_id': item['track_id'], 'proposal_id': proposal_id})
                    entries.append({'track_id': item['track_id'], 'input_sha256': item['input_sha256'], 'proposal_id': proposal_id, 'state': 'proposed', 'changed_fields': [change['field'] for change in item['changes']], 'skipped_fields': item['skipped_fields']})
                result = {'id': identity, 'created_at': now, 'release_review_id': body.release_review_id, 'release_review_version': body.release_review_version,
                    'entries': entries, 'excluded': plan['excluded'], 'audio_changed': False, 'apply_job_created': False}
                conn.execute('INSERT INTO settings VALUES(?,?)', ('album_batch:' + identity, db.encode(result)))
                conn.execute('INSERT INTO settings VALUES(?,?)', ('album_batch_idempotency:' + key_hash, db.encode({'request_hash': request_hash, 'batch_id': identity})))
                db.event(conn, 'album_batch_saved', {'batch_id': identity, 'proposals': sum(entry['state'] == 'proposed' for entry in entries)})
                return result
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc

    @app.get('/api/album-batches/{identity}')
    def detail(request: Request, identity: str):
        security.require_session(request, settings, owner_only=True)
        try:
            identity = str(uuid.UUID(identity))
        except ValueError as exc:
            raise security.error(404, 'album_batch_missing', 'Пакет не найден') from exc
        value = db.setting(settings.db_path, 'album_batch:' + identity)
        if not value:
            raise security.error(404, 'album_batch_missing', 'Пакет не найден')
        return json.loads(value)
