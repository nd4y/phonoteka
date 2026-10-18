"""Explicit release comparison, never an album-tag completeness assumption."""
import csv
import hashlib
import io
import json
import time
import uuid

from fastapi import Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import db, media, provider_policy, security
from .providers import POLICY_VERSION, Providers, ProviderError, normalized, scalar

PREFIX = 'release_review:'
MAX_FILES, MAX_DISCS, MAX_TRACKS = 200, 50, 1000


def release_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise media.MediaError('invalid_release_id', 'Нужен UUID конкретного MusicBrainz release') from exc


def validate_request(params, ids):
    release_id(params.get('release_id'))
    if not 1 <= len(ids) <= MAX_FILES:
        raise media.MediaError('invalid_release_members', 'Выберите от 1 до 200 файлов для сверки релиза')


def credit(values):
    return ''.join(str(value.get('name') or value.get('artist', {}).get('name') or '') + str(value.get('joinphrase') or '') for value in values or [] if isinstance(value, dict))[:1000]


def reference(document, identity):
    """Reject truncated/ambiguous reference structure instead of inventing gaps."""
    if not isinstance(document, dict) or document.get('id') != identity:
        raise media.MediaError('release_reference_invalid', 'MusicBrainz вернул другой релиз или некорректный ответ')
    media_rows = document.get('media')
    if not isinstance(media_rows, list) or not 1 <= len(media_rows) <= MAX_DISCS:
        raise media.MediaError('release_reference_incomplete', 'Полный список дисков релиза недоступен')
    declared = document.get('medium-count', document.get('media-count', len(media_rows)))
    if declared != len(media_rows):
        raise media.MediaError('release_reference_incomplete', 'Ответ содержит не все диски релиза')
    tracks, slots, discs = [], set(), set()
    for medium in media_rows:
        if not isinstance(medium, dict):
            raise media.MediaError('release_reference_invalid', 'Некорректное описание диска')
        disc = medium.get('position')
        values = medium.get('tracks')
        if type(disc) is not int or not 1 <= disc <= MAX_DISCS or disc in discs or not isinstance(values, list) or not values or medium.get('track-count', len(values)) != len(values):
            raise media.MediaError('release_reference_incomplete', 'Список композиций диска неполон или неоднозначен')
        discs.add(disc)
        for entry in values:
            if not isinstance(entry, dict):
                raise media.MediaError('release_reference_invalid', 'Некорректное описание композиции')
            position = entry.get('position')
            if type(position) is not int or not 1 <= position <= MAX_TRACKS:
                raise media.MediaError('release_reference_invalid', 'В эталоне некорректная позиция композиции')
            key = f'{disc}:{position}'
            if key in slots:
                raise media.MediaError('release_reference_invalid', 'В эталоне повторяются позиции')
            slots.add(key)
            recording = entry.get('recording') or {}
            if not isinstance(recording, dict):
                raise media.MediaError('release_reference_invalid', 'Некорректный идентификатор записи')
            length = entry.get('length') or recording.get('length')
            duration = float(length) / 1000 if isinstance(length, (int, float)) and 0 < length < 86400000 else None
            tracks.append({'slot': key, 'disc': disc, 'position': position, 'number': str(entry.get('number') or position)[:50],
                'title': str(entry.get('title') or recording.get('title') or '')[:1000],
                'artist': credit(entry.get('artist-credit') or recording.get('artist-credit') or document.get('artist-credit')),
                'duration': duration, 'recording_id': recording.get('id'), 'release_track_id': entry.get('id')})
            if len(tracks) > MAX_TRACKS:
                raise media.MediaError('release_reference_too_large', 'Релиз превышает профиль 1000 композиций')
    return {'id': identity, 'title': str(document.get('title') or '')[:1000], 'artist': credit(document.get('artist-credit')),
        'date': str(document.get('date') or '')[:50], 'country': str(document.get('country') or '')[:50],
        'disambiguation': str(document.get('disambiguation') or '')[:1000], 'status': str(document.get('status') or '')[:100],
        'discs': len(discs), 'tracks': tracks, 'source_url': 'https://musicbrainz.org/release/' + identity}


def position(value):
    head = str(value or '').split('/')[0].strip()
    return int(head) if head.isdigit() and 0 < int(head) <= MAX_TRACKS else None


def compare(ref, rows, expected_slots=None):
    slots = {entry['slot']: entry for entry in ref['tracks']}
    expected = set(slots if expected_slots is None else expected_slots)
    if not expected or expected - set(slots):
        raise media.MediaError('invalid_expected_subset', 'Выберите существующие композиции выбранного релиза')
    findings = {key: [] for key in slots}
    extras, unresolved = [], []
    for row in rows:
        meta = json.loads(row['metadata']); tags = meta.get('tags', {})
        item = {'track_id': row['id'], 'relative_path': row['relative_path'], 'input_sha256': row['sha256'], 'reasons': []}
        disc, track = position(scalar(tags, 'discnumber')), position(scalar(tags, 'tracknumber'))
        if disc is None and ref['discs'] == 1:
            disc = ref['tracks'][0]['disc']
        key = f'{disc}:{track}' if disc and track else None
        recording_id = scalar(tags, 'musicbrainz_trackid')
        release_track_id = scalar(tags, 'musicbrainz_releasetrackid')
        by_track = [entry['slot'] for entry in ref['tracks'] if release_track_id and entry.get('release_track_id') == release_track_id]
        by_recording = [entry['slot'] for entry in ref['tracks'] if recording_id and entry.get('recording_id') == recording_id]
        if len(by_track) == 1:
            target = by_track[0]
            if key and key != target:
                item['reasons'].append('track_position_differs')
            key = target
        elif key not in slots and len(by_recording) == 1:
            key = by_recording[0]
            item['reasons'].append('track_position_missing_or_differs')
        if key not in slots:
            item['reasons'].append('position_not_in_release' if track and disc else 'position_unknown')
            (extras if track and disc else unresolved).append(item)
            continue
        entry = slots[key]
        if scalar(tags, 'musicbrainz_albumid') and scalar(tags, 'musicbrainz_albumid') != ref['id']:
            item['reasons'].append('different_release_id')
        if release_track_id and entry.get('release_track_id') and release_track_id != entry['release_track_id']:
            item['reasons'].append('release_track_id_differs')
        if recording_id and entry.get('recording_id') and recording_id != entry['recording_id']:
            item['reasons'].append('recording_id_differs')
        title, artist = scalar(tags, 'title'), scalar(tags, 'artist')
        if not title:
            item['reasons'].append('title_missing')
        elif entry['title'] and normalized(title) != normalized(entry['title']):
            item['reasons'].append('title_differs')
        if not artist:
            item['reasons'].append('artist_missing')
        elif entry['artist'] and normalized(artist) != normalized(entry['artist']):
            item['reasons'].append('artist_differs')
        duration = meta.get('duration')
        delta = abs(float(duration) - entry['duration']) if isinstance(duration, (int, float)) and duration > 0 and entry['duration'] else None
        if delta is None:
            item['reasons'].append('duration_unknown')
        elif delta > 2:
            item['reasons'].append('duration_differs')
        item.update(slot=key, duration_delta=round(delta, 3) if delta is not None else None,
            evidence='release_track_id' if release_track_id and release_track_id == entry.get('release_track_id') else 'recording_id_and_position' if recording_id and recording_id == entry.get('recording_id') else 'owner_selected_release_and_metadata',
            state='conflict' if item['reasons'] else 'matched')
        findings[key].append(item)
    output = [{**entry, 'expected': key in expected, 'files': findings[key],
        'state': 'not_expected' if key not in expected else 'missing' if not findings[key] else 'duplicate' if len(findings[key]) > 1 else findings[key][0]['state']} for key, entry in slots.items()]
    missing = [entry['slot'] for entry in output if entry['expected'] and not entry['files']]
    conflicts = [entry['slot'] for entry in output if entry['expected'] and (len(entry['files']) > 1 or any(item['reasons'] for item in entry['files']))]
    if unresolved:
        state = 'unknown'
    elif conflicts or extras:
        state = 'needs_review'
    elif missing:
        state = 'subset_missing' if expected_slots is not None else 'missing'
    else:
        state = 'subset_complete' if expected_slots is not None else 'complete'
    return {'state': state, 'tracks': output, 'missing_slots': missing, 'conflict_slots': conflicts, 'extra_files': extras, 'unresolved_files': unresolved,
        'expected_positions': sorted(expected), 'intentional_subset': expected_slots is not None, 'complete_release': state == 'complete',
        'matched_track_ids': [entry['files'][0]['track_id'] for entry in output if entry['expected'] and len(entry['files']) == 1 and not entry['files'][0]['reasons']],
        'counts': {'expected': len(expected), 'found_slots': sum(bool(findings[key]) for key in expected), 'missing': len(missing), 'conflicts': len(conflicts), 'extra': len(extras), 'unresolved': len(unresolved)},
        'identity_verified_by_audio': False, 'duration_tolerance_seconds': 2}


def redact(conn, value):
    result = json.loads(db.encode(value))
    if result.get('reference') and (time.time() >= result.get('usage', {}).get('expires_at', 0) or not provider_policy._allowed(conn, result.get('usage', {}), 'cache')):
        result.pop('reference', None)
        result['summary'] = {'state': 'unknown'}
        result['reference_state'] = 'reference_expired_or_policy_denied'
    return result


def reference_usage(settings, url, params):
    usage = provider_policy.get_rule(settings, 'musicbrainz', 'metadata')
    key = hashlib.sha256(db.encode([POLICY_VERSION, usage['policy_version'], url, params, False]).encode()).hexdigest()
    cached = db.fetchone(settings.db_path, 'SELECT expires FROM provider_cache WHERE key=?', (key,))
    # A cache hit is not a new acquisition. Never extend the original TTL by
    # copying an old provider response into a new owner review.
    expires = cached['expires'] if cached else time.time()
    usage.update(origin='provider_acquired', acquired_at=expires - min(usage['cache_ttl'], 7 * 86400), expires_at=expires)
    return usage


def stored(conn, identity):
    try:
        identity = str(uuid.UUID(identity))
    except ValueError as exc:
        raise security.error(404, 'release_review_missing', 'Сверка релиза не найдена') from exc
    row = conn.execute('SELECT value FROM settings WHERE key=?', (PREFIX + identity,)).fetchone()
    if not row:
        raise security.error(404, 'release_review_missing', 'Сверка релиза не найдена')
    return redact(conn, json.loads(row['value']))


def report(conn, value):
    value = redact(conn, value)
    rows, stale = [], []
    for member in value['members']:
        current = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (member['track_id'],)).fetchone()
        if not current or current['sha256'] != member['sha256'] or current['status'] == 'missing':
            stale.append(member['track_id'])
        if current:
            rows.append(dict(current))
    result = {**value, 'stale': bool(stale), 'stale_track_ids': stale}
    if value.get('reference'):
        result.update(compare(value['reference'], rows, value.get('expected_slots')))
    else:
        result.update(state='unknown', tracks=[], missing_slots=[], conflict_slots=[], extra_files=[], unresolved_files=[], counts={'expected': 0, 'found_slots': 0, 'missing': 0, 'conflicts': 0, 'extra': 0, 'unresolved': 0}, matched_track_ids=[], complete_release=False)
    if stale:
        result.update(state='unknown', complete_release=False)
    return result


def persist(settings, job, items, ref, usage, authorize):
    authorize()
    with db.transaction(settings.db_path) as conn:
        for item in items:
            current = conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1', (item['track_id'],)).fetchone()
            if not current or current['sha256'] != item['sha256']:
                raise media.MediaError('conflict', 'Состав альбома изменился во время сверки')
        if time.time() >= usage.get('expires_at', 0) or not provider_policy._allowed(conn, usage, 'cache'):
            raise media.MediaError('provider_policy_denied', 'Хранение списка релиза не разрешено политикой источника')
        value = {'id': job['id'], 'version': 1, 'release_id': ref['id'], 'reference': ref, 'usage': usage, 'reference_state': 'available',
            'members': [{'track_id': item['track_id'], 'sha256': item['sha256'], 'relative_path': item['relative_path']} for item in items],
            'expected_slots': None, 'subset_note': '', 'created_at': db.utcnow(), 'updated_at': db.utcnow()}
        result = report(conn, value)
        value['summary'] = {key: result[key] for key in ('state', 'counts', 'complete_release')}
        value['summary'].update(title=ref['title'], artist=ref['artist'])
        conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (PREFIX + job['id'], db.encode(value)))
        db.event(conn, 'release_review_created', {'job_id': job['id'], 'review_id': job['id'], 'state': result['state']})
    return {'release_review_id': job['id'], 'release_id': ref['id'], 'state': result['state'], 'counts': result['counts']}


async def process(worker, job):
    payload = json.loads(job['payload']); items = payload['items']; identity = release_id(payload['params']['release_id'])
    def authorized():
        if not worker.settings.external_metadata or worker.cancelled(job['id']) or int(db.setting(worker.settings.db_path, 'auth_epoch', '0')) != job['auth_epoch'] or db.setting(worker.settings.db_path, 'recovery_required', 'false') == 'true':
            raise media.MediaError('authorization_revoked', 'Разрешение сверки отозвано')
    try:
        for item in items:
            await worker.io(authorized)
            await worker.io(worker.item_result, job['id'], item['track_id'], 'running', {'operation': 'release_reference'})
            await worker.io(media._expect, worker.settings.work_root / item['relative_path'], item['sha256'])
            row = await worker.io(db.fetchone, worker.settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],))
            if not row or row['sha256'] != item['sha256']:
                raise media.MediaError('conflict', 'Версия файла изменилась до сверки')
            await worker.io(provider_policy.require_track, worker.settings, row, 'derived_analysis')
        if worker.providers is None:
            worker.providers = Providers(worker.settings, metrics=worker.metrics)
        await worker.io(authorized)
        url = 'https://' + worker.settings.musicbrainz_host + '/ws/2/release/' + identity
        params = {'inc': 'recordings+artist-credits', 'fmt': 'json'}
        document = await worker.providers.get('musicbrainz', url, params)
        if document is None:
            raise media.MediaError('release_not_found', 'Этот MusicBrainz release не найден; комплектность неизвестна')
        ref = await worker.io(reference, document, identity)
        usage = await worker.io(reference_usage, worker.settings, url, params)
        result = await worker.io(persist, worker.settings, job, items, ref, usage, authorized)
        for item in items:
            await worker.io(worker.item_result, job['id'], item['track_id'], 'succeeded', result)
        canceled = await worker.io(worker.cancelled, job['id'])
        await worker.io(worker.finish, job['id'], 'cancelled_partial' if canceled else 'succeeded')
    except ProviderError as exc:
        canceled = await worker.io(worker.cancelled, job['id'])
        if exc.code in {'rate_limited', 'provider_unavailable', 'provider_cooldown', 'budget_exhausted'} and not canceled:
            if exc.code == 'provider_unavailable':
                await worker.io(worker.providers.unavailable, 'musicbrainz')
            breaker = json.loads(await worker.io(db.setting, worker.settings.db_path, 'provider_breaker:musicbrainz', '{}'))
            retry = max(time.time() + 10, breaker.get('until', time.time() + 60))
            if exc.code == 'budget_exhausted':
                first = await worker.io(db.fetchone, worker.settings.db_path, "SELECT MIN(at) AS at FROM provider_calls WHERE provider='musicbrainz'")
                retry = max(retry, (first['at'] if first and first['at'] else time.time()) + 86401)
            payload['retry_at'] = retry
            def wait():
                with db.transaction(worker.settings.db_path) as conn:
                    conn.execute("UPDATE jobs SET state='queued',stage='waiting_provider',payload=?,updated_at=? WHERE id=?", (db.encode(payload), db.utcnow(), job['id']))
                    db.event(conn, 'job_waiting', {'job_id': job['id'], 'retry_at': retry})
            await worker.io(wait)
            for item in items:
                await worker.io(worker.item_result, job['id'], item['track_id'], 'deferred', {'code': exc.code, 'retry_at': retry})
            return
        for item in items:
            await worker.io(worker.item_result, job['id'], item['track_id'], 'cancelled' if canceled else 'failed', {'code': exc.code, 'message': str(exc), 'retryable': False})
        await worker.io(worker.finish, job['id'], 'cancelled' if canceled else 'failed', exc.code)
    except Exception as exc:
        canceled = await worker.io(worker.cancelled, job['id'])
        for item in items:
            await worker.io(worker.item_result, job['id'], item['track_id'], 'failed' if not canceled else 'cancelled', worker.failure(exc))
        await worker.io(worker.finish, job['id'], 'cancelled' if canceled else 'failed', worker.failure(exc)['code'])


class SubsetInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=1)
    expected_slots: list[str] | None = Field(default=None, max_length=MAX_TRACKS)
    note: str = Field(default='', max_length=1000)


def install(app, settings):
    @app.get('/api/release-reviews')
    def listing(request: Request):
        security.require_session(request, settings)
        with db.connect(settings.db_path) as conn:
            rows = conn.execute("SELECT value FROM settings WHERE key LIKE 'release_review:%' ORDER BY json_extract(value,'$.created_at') DESC LIMIT 50").fetchall()
            items = []
            for row in rows:
                value = redact(conn, json.loads(row['value']))
                stale = False
                for member in value['members']:
                    current = conn.execute('SELECT sha256,status FROM tracks WHERE id=? AND is_current=1', (member['track_id'],)).fetchone()
                    if not current or current['sha256'] != member['sha256'] or current['status'] == 'missing':
                        stale = True
                        break
                summary = value.get('summary') if value.get('reference') and not stale else {'state': 'unknown'}
                items.append({key: value[key] for key in ('id', 'version', 'release_id', 'created_at', 'updated_at')} | {'summary': summary, 'stale': stale, 'reference_state': value['reference_state'], 'files': len(value['members'])})
        return {'items': items, 'limit': 50}

    @app.get('/api/release-reviews/members')
    def members(request: Request, ids: str = Query(max_length=7500)):
        security.require_session(request, settings)
        values = list(dict.fromkeys(ids.split(',')))
        if not 1 <= len(values) <= MAX_FILES:
            raise security.error(422, 'invalid_release_members', 'Выберите от 1 до 200 файлов')
        entries = []
        with db.connect(settings.db_path) as conn:
            for identity in values:
                row = conn.execute('SELECT id,relative_path,metadata FROM tracks WHERE id=? AND is_current=1', (identity,)).fetchone()
                if not row:
                    raise security.error(404, 'track_not_found', 'Файл группы недоступен')
                tags = json.loads(row['metadata']).get('tags', {})
                entries.append({'id': row['id'], 'path': row['relative_path'], **{field: scalar(tags, field) for field in ('title', 'artist', 'tracknumber', 'discnumber')}, 'release_id': scalar(tags, 'musicbrainz_albumid')})
        return {'items': entries}

    @app.get('/api/release-reviews/{identity}')
    def detail(request: Request, identity: str):
        security.require_session(request, settings)
        with db.connect(settings.db_path) as conn:
            return report(conn, stored(conn, identity))

    @app.put('/api/release-reviews/{identity}')
    def subset(request: Request, identity: str, body: SubsetInput):
        security.require_session(request, settings, True, owner_only=True)
        if body.expected_slots is not None and (not body.expected_slots or len(body.expected_slots) != len(set(body.expected_slots)) or len(body.note.strip()) < 5):
            raise security.error(422, 'subset_note_required', 'Для намеренного подмножества выберите композиции и укажите причину')
        with db.transaction(settings.db_path) as conn:
            value = stored(conn, identity); current = report(conn, value)
            if current['stale'] or value['version'] != body.base_version:
                raise security.error(412, 'release_review_stale', 'Состав или решение изменились; выполните новую сверку')
            if not value.get('reference'):
                raise security.error(409, 'reference_unavailable', 'Нужен свежий эталонный список релиза')
            allowed = {entry['slot'] for entry in value['reference']['tracks']}
            if body.expected_slots is not None and set(body.expected_slots) - allowed:
                raise security.error(422, 'invalid_expected_subset', 'Позиции не относятся к выбранному релизу')
            value.update(expected_slots=body.expected_slots, subset_note=body.note.strip(), version=value['version'] + 1, updated_at=db.utcnow())
            result = report(conn, value)
            value['summary'] = {key: result[key] for key in ('state', 'counts', 'complete_release')}
            value['summary'].update(title=value['reference']['title'], artist=value['reference']['artist'])
            conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (PREFIX + value['id'], db.encode(value)))
            decision = {key: value[key] for key in ('version', 'expected_slots', 'subset_note', 'updated_at')}
            conn.execute('INSERT INTO settings VALUES(?,?)', ('release_review_decision:' + value['id'] + ':' + str(value['version']), db.encode(decision)))
            db.event(conn, 'release_subset_changed', {'review_id': value['id'], 'version': value['version']})
            return result

    @app.get('/api/release-reviews/{identity}/export/{kind}')
    def export(request: Request, identity: str, kind: str):
        security.require_session(request, settings)
        if kind not in {'json', 'csv'}:
            raise security.error(404, 'export_missing', 'Формат отчета не найден')
        with db.connect(settings.db_path) as conn:
            value = report(conn, stored(conn, identity))
        if kind == 'json':
            content, mime = json.dumps(value, ensure_ascii=False, indent=2), 'application/json'
        else:
            output = io.StringIO(); writer = csv.writer(output); writer.writerow(['release_id', 'slot', 'title', 'artist', 'expected', 'state', 'track_ids', 'reasons'])
            for entry in value['tracks']:
                row = [value['release_id'], entry['slot'], entry['title'], entry['artist'], entry['expected'], entry['state'], ','.join(item['track_id'] for item in entry['files']), ','.join(sorted({reason for item in entry['files'] for reason in item['reasons']}))]
                writer.writerow(["'" + item if isinstance(item, str) and item.lstrip().startswith(('=', '+', '-', '@')) else item for item in row])
            content, mime = output.getvalue(), 'text/csv; charset=utf-8'
        return Response(content, media_type=mime, headers={'Content-Disposition': 'attachment; filename="release-review.' + kind + '"'})
