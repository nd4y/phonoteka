"""Owner artwork transform previews; source images never persist as a cache."""
import base64
from collections import OrderedDict, deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from . import db, media, media_slots, provider_policy, security

MAX_SOURCE_BYTES = 20 * 1024 * 1024
SLOTS = threading.BoundedSemaphore(1)
ADMISSION = threading.Condition()
WAITERS = deque()
VALIDATED = OrderedDict()
CACHE_LOCK = threading.Lock()
PREFIX = 'artwork_preview:'


def acquire_slot():
    ticket = object(); deadline = time.monotonic() + 10
    with ADMISSION:
        if len(WAITERS) >= 32: return False
        WAITERS.append(ticket)
        while WAITERS[0] is not ticket or not SLOTS.acquire(blocking=False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                WAITERS.remove(ticket); ADMISSION.notify_all(); return False
            ADMISSION.wait(remaining)
        WAITERS.popleft()
        return True


def release_slot():
    with ADMISSION:
        SLOTS.release(); ADMISSION.notify_all()


def cached_validation(sha):
    with CACHE_LOCK:
        found = VALIDATED.get(sha)
        if found: VALIDATED.move_to_end(sha); return {'source': dict(found)}


def process(raw, options=None):
    if not isinstance(raw, bytes) or len(raw) > MAX_SOURCE_BYTES: raise media.MediaError('invalid_cover', 'Обложка превышает 20 МиБ')
    sha = hashlib.sha256(raw).hexdigest()
    if options is None:
        cached = cached_validation(sha)
        if cached: return cached
    if not acquire_slot(): raise media.MediaError('artwork_busy', 'Очередь декодирования занята; повторите предпросмотр', retryable=True)
    try:
        if options is None:
            cached = cached_validation(sha)
            if cached: return cached
        try:
            completed = subprocess.run([sys.executable, str(Path(__file__).with_name('artwork_worker.py'))],
                input=json.dumps({'image_b64': base64.b64encode(raw).decode(), 'options': options}).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
            result = json.loads(completed.stdout) if completed.returncode == 0 else {}
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            raise media.MediaError('invalid_cover', 'Превышен предел времени или ресурсов декодирования изображения') from exc
        if not result or result.get('error'): raise media.MediaError('invalid_cover', 'Нужен корректный JPEG/PNG до 20 МиБ и 25 мегапикселей')
        with CACHE_LOCK:
            VALIDATED[sha] = {key: result['source'][key] for key in ('sha256', 'bytes', 'width', 'height', 'mime')}
            VALIDATED.move_to_end(sha)
            while len(VALIDATED) > 512: VALIDATED.popitem(last=False)
        return result
    finally: release_slot()


class ArtworkInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source: str = Field(max_length=28 * 1024 * 1024)
    slot_selection: media_slots.SlotSelection
    max_edge: int = Field(default=1200, ge=64, le=5000)
    max_bytes: int = Field(default=1536 * 1024, ge=32768, le=10 * 1024 * 1024)
    format: Literal['jpeg', 'png'] = 'jpeg'
    quality: int = Field(default=88, ge=40, le=95)


def proposal(conn, row, payload, preview_id, confirm_smaller=False):
    if not preview_id: return payload
    stored = conn.execute('SELECT value FROM settings WHERE key=?', (PREFIX + preview_id,)).fetchone()
    preview = json.loads(stored[0]) if stored else None
    if not preview or preview['expires_at'] < time.time() or preview['track_id'] != row['id'] or preview['file_revision'] != row['sha256']:
        raise security.error(412, 'artwork_preview_stale', 'Повторите предпросмотр обложки для текущей версии файла')
    selected_cover = {key: value for key, value in payload.get('slot_selection', {}).items() if key in {'inventory_sha256', 'cover_slot_id'}}
    if selected_cover != preview['slot_selection'] or not payload.get('cover'):
        raise security.error(412, 'artwork_preview_stale', 'Выбранный слот не совпадает с предпросмотром')
    try: sha = media_slots.metadata_slots.digest(base64.b64decode(payload['cover'].split(',', 1)[1], validate=True))
    except (ValueError, IndexError): raise security.error(422, 'invalid_cover', 'Некорректное изображение')
    if sha != preview['result']['sha256']: raise security.error(412, 'artwork_preview_stale', 'Изображение отличается от результата предпросмотра')
    if preview['smaller_than_existing'] and not confirm_smaller:
        raise security.error(409, 'smaller_cover_confirmation_required', 'Подтвердите уменьшение относительно существующей обложки')
    payload.setdefault('field_sources', {})['cover'] = preview['field_source']
    payload['artwork_transform'] = {key: preview[key] for key in ('source', 'result', 'profile', 'slot_selection')}
    payload['artwork_transform']['smaller_confirmed_by_owner'] = bool(confirm_smaller)
    return payload


def install(app, settings):
    @app.post('/api/tracks/{track_id}/artwork-preview')
    def preview(request: Request, track_id: str, body: ArtworkInput):
        security.require_session(request, settings, True, owner_only=True)
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,))
        if not row: raise security.error(404, 'track_not_found', 'Трек не найден')
        if request.headers.get('if-match', '').strip('"') != row['sha256']: raise security.error(412, 'revision_conflict', 'Файл изменился')
        if not body.source.startswith(('data:image/jpeg;base64,', 'data:image/png;base64,')): raise security.error(422, 'invalid_cover', 'Нужен JPEG или PNG')
        try: raw = base64.b64decode(body.source.split(',', 1)[1], validate=True)
        except ValueError: raise security.error(422, 'invalid_cover', 'Некорректный base64')
        choice = body.slot_selection.model_dump(exclude_none=True, include={'inventory_sha256', 'cover_slot_id'})
        if not choice.get('cover_slot_id'): raise security.error(409, 'cover_conflict', 'Сначала выберите целевой слот обложки')
        try:
            audio, _ = media_slots.read(settings, row)
            selected = media_slots.metadata_slots.targets(audio, choice, cover=True)['cover']
            with db.connect(settings.db_path) as conn:
                source = provider_policy.require_payload(conn, row, {'cover': body.source}, 'derived_analysis')
                # A previous field's provenance is conservative inheritance,
                # never authority to ignore stricter rights registered for the
                # newly uploaded value itself. Preserve both constraint sets.
                incoming_hash = provider_policy.digest(provider_policy.field_values({'cover': body.source})['cover'])
                incoming = conn.execute('SELECT value FROM settings WHERE key=?', ('usage_value:cover:' + incoming_hash,)).fetchone()
                usages = deepcopy(provider_policy.references(source)) + (json.loads(incoming[0]).get('entries', []) if incoming else [])
                provider_policy.require_sources(conn, usages, 'derived_analysis')
                provider_policy.require_sources(conn, usages, 'cache')
                if usages: source['field_sources']['cover']['usage'] = usages
            result = process(raw, body.model_dump(include={'max_edge', 'max_bytes', 'format', 'quality'}))
            if body.source.split(';', 1)[0][5:] != result['source']['mime']: raise media.MediaError('invalid_cover', 'MIME исходного изображения не соответствует содержимому')
            existing = None
            if selected:
                if selected.get('_picture'): old_raw = selected['_picture'].data
                elif selected.get('_frame'): old_raw = selected['_frame'].data
                else: old_raw = bytes(audio['covr'][selected['_index']])
                try: existing = process(old_raw)['source']
                except media.MediaError: existing = None
        except media.MediaError as exc: raise security.error(429 if exc.code == 'artwork_busy' else 409, exc.code, exc.message) from exc
        smaller = bool(existing and (result['result']['width'] * result['result']['height'] < existing['width'] * existing['height']))
        identity = str(uuid.uuid4()); now = time.time()
        record = {key: result[key] for key in ('source', 'result', 'profile')}
        record.update(track_id=track_id, file_revision=row['sha256'], slot_selection=choice, expires_at=now + 900,
                      smaller_than_existing=smaller, field_source=source['field_sources']['cover'])
        encoded = result.pop('image_b64')
        result_cover = 'data:' + result['result']['mime'] + ';base64,' + encoded
        with db.transaction(settings.db_path) as conn:
            conn.execute("DELETE FROM settings WHERE key LIKE 'artwork_preview:%' AND json_extract(value,'$.expires_at')<?", (now,))
            if conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'artwork_preview:%'").fetchone()[0] >= 100:
                raise security.error(429, 'artwork_preview_limit', 'Слишком много действующих предпросмотров')
            conn.execute('INSERT INTO settings VALUES(?,?)', (PREFIX + identity, db.encode(record)))
            for usage in provider_policy.references(source):
                if usage.get('origin') == 'provider_acquired' and usage.get('provider') and usage.get('policy_version'):
                    provider_policy.register_values(conn, {'cover': result_cover}, usage)
        return {**result, 'preview_id': identity, 'existing': existing, 'smaller_than_existing': smaller, 'expires_at': now + 900,
                'cover': result_cover}
