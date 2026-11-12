"""Fill independently supported metadata gaps without accepting lyric drafts.

This deterministic policy makes no ASR/music-pilot claim. The existing automatic
lyric gate remains separate. Candidates, permissions and every file dependency
are resolved again at enqueue and immediately before the native atomic write.
"""
import hashlib
import json
import math
import re

from fastapi import Query, Request

from . import catalog_sources, db, enrichment_gaps, field_policy, input_readiness, lyrics_document, media, provider_policy, recognition, security
from .providers import normalized, scalar

CONTRACT = 'confirmed-metadata-fields-v1'
MODE = 'confirmed_fields'
FIELDS = ('title', 'artist', 'album', 'cover')
TERMINAL = ('succeeded', 'failed', 'cancelled', 'cancelled_partial', 'completed_with_errors')
UUID = re.compile(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}')
HASH = re.compile(r'[a-f0-9]{64}')
MESSAGES = {
    'recording_unconfirmed': 'Нет однозначного совпадения аудио и музыкального каталога.',
    'recording_conflict': 'Источники или данные файла указывают на разные записи.',
    'album_unconfirmed': 'Для этой записи найдены разные альбомы или альбом не установлен.',
    'cover_edition_unconfirmed': 'Не подтверждена обложка подходящего издания.',
    'candidate_unavailable': 'Подходящего доступного варианта для этого поля пока нет.',
    'provider_usage_denied': 'Источник ограничивает использование этих данных или срок хранения истёк.',
    'field_locked': 'Поле закреплено пользователем.',
    'pending_choice': 'В сохранённом предложении выбран другой вариант этого поля.',
    'analysis_stale': 'Поиск относится к прежней версии файла. Нужны актуальные результаты.',
    'analysis_missing': 'Сначала нужен поиск данных для этой записи.',
    'invalid_evidence': 'Результаты источников повреждены или имеют неподдерживаемый формат.',
    'audio_audit_required': 'Нужна успешная проверка текущего аудиофайла.',
    'input_stabilizing': 'Файл ещё копируется или не подтверждена его стабильность.',
    'file_unavailable': 'Файл недоступен, имеет конфликт или не поддерживает запись.',
    'excluded_recording': 'Миксы, разговорные записи и исходные альбомные образы пропускаются.',
    'unresolved_task': 'У записи есть незавершённая задача. Сначала проверьте её результат.',
    'field_lock_stale': 'Изменилось закреплённое значение. Требуется решение пользователя.',
    'read_only': 'Запись отключена в настройках хранилища.',
    'authorization_required': 'Для записи требуется действующее разрешение.',
    'recovery_required': 'Сначала нужно завершить восстановление файловых операций.',
    'cover_slot_unconfirmed': 'Не определён безопасный слот лицевой обложки.',
    'lyrics_review_required': 'Новый текст и таймкоды остаются на проверке; неполный ASR не принимается автоматически.',
}


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def permitted(conn, candidate, operations=('cache', 'embed')):
    usage = candidate.get('usage')
    return (isinstance(usage, dict) and usage.get('provider') == candidate.get('provider')
            and all(provider_policy._allowed(conn, usage, operation) for operation in operations))


def recording(conn, analysis, metadata):
    candidates = analysis.get('candidates', [])
    fps = [c for c in candidates if c.get('identification') == 'audio_fingerprint'
           and c.get('match', {}).get('state') == 'fingerprint_match'
           and permitted(conn, c, ('cache', 'derived_analysis'))]
    acoustid = [c for c in fps if c.get('provider') == 'acoustid' and number(c.get('score'))]
    high = [c for c in acoustid if c['score'] >= .98]
    aid_ids = {c.get('recording_id') for c in high}
    acoustic = []
    if len(aid_ids) == 1 and all(c.get('recording_id') in aid_ids or c['score'] < .9 for c in acoustid):
        acoustic = [c for c in high if UUID.fullmatch(str(c.get('recording_id', '')))]
    segments = analysis.get('recognition_segments') or []
    shazam = []
    duration = metadata.get('duration')
    if (analysis.get('recording_confirmation') == 'agreement' and 2 <= len(segments) <= 3
            and number(duration) and duration > 0
            and all(s.get('state') == 'match' and s.get('provider') == 'shazam'
                    and s.get('extractor_profile') == recognition.POLICY
                    and number(s.get('offset')) and 0 <= s['offset'] < duration
                    and HASH.fullmatch(str(s.get('sample_sha256', ''))) for s in segments)
            and max(s['offset'] for s in segments) - min(s['offset'] for s in segments) >= 5
            and len({s['sample_sha256'] for s in segments}) == len(segments)):
        for c in fps:
            if c.get('provider') != 'shazam' or c.get('extractor_profile') != recognition.POLICY: continue
            ids = [(s.get('candidate') or {}).get('provider_recording_id') or s.get('provider_recording_id') for s in segments]
            bound = any(s['sample_sha256'] == c.get('sample_sha256') and s['offset'] == c.get('sample_offset') for s in segments)
            if bound and c.get('provider_recording_id') and set(ids) == {c['provider_recording_id']}:
                shazam.append(c)
    fingerprints = acoustic + shazam
    if not fingerprints: return [], [], 'recording_unconfirmed'
    identities = {(normalized(scalar(c.get('tags', {}), 'title')), normalized(scalar(c.get('tags', {}), 'artist'))) for c in fingerprints}
    if len(identities) != 1 or any(not title or not artist for title, artist in identities):
        return [], [], 'recording_conflict'
    title, artist = next(iter(identities))
    current = metadata.get('tags', {})
    if any(scalar(current, f).strip() and normalized(scalar(current, f)) != expected
           for f, expected in (('title', title), ('artist', artist))):
        return [], [], 'recording_conflict'
    matches = [c for c in candidates if c.get('provider') == 'musicbrainz' and c.get('kind') == 'metadata'
               and permitted(conn, c, ('cache', 'derived_analysis')) and UUID.fullmatch(str(c.get('recording_id', '')))
               and normalized(scalar(c.get('tags', {}), 'title')) == title
               and normalized(scalar(c.get('tags', {}), 'artist')) == artist
               and number(duration) and number(c.get('duration')) and duration > 0 and c['duration'] > 0
               and abs(duration - c['duration']) <= 2
               and (not acoustic or c['recording_id'] in aid_ids)]
    if not matches: return [], fingerprints, 'recording_unconfirmed'
    return matches, fingerprints, None


def album_and_cover(conn, metadata, candidates, recordings):
    releases = {}
    selected_id = scalar(metadata.get('tags', {}), 'musicbrainz_albumid')
    if not selected_id and any(c.get('releases_truncated') or len(c.get('releases', [])) >= 15 for c in recordings):
        return None, None
    for c in recordings:
        for r in c.get('releases', []):
            if (UUID.fullmatch(str(r.get('id', ''))) and str(r.get('title', '')).strip()
                    and r.get('status') == 'Official'):
                if r['id'] in releases and normalized(releases[r['id']]) != normalized(r['title']):
                    return None, None
                releases.setdefault(r['id'], r['title'])
    names = {normalized(title) for title in releases.values()}
    album = scalar(metadata.get('tags', {}), 'album').strip()
    if not album:
        if selected_id in releases: album = releases[selected_id]
        elif len(names) == 1: album = next(iter(releases.values()))
    release_ids = {rid for rid, title in releases.items() if normalized(title) == normalized(album)} if album else set()
    if selected_id:
        release_ids &= {selected_id}
    albums = [c for c in candidates if c.get('provider') == 'musicbrainz' and c.get('kind') == 'release'
              and c.get('release_id') in release_ids and normalized(scalar(c.get('tags', {}), 'album')) == normalized(album)]
    covers = [c for c in candidates if c.get('provider') == 'coverartarchive' and c.get('kind') == 'cover'
              and c.get('release_id') in release_ids and c.get('cover') and permitted(conn, c)]
    # A country/date distinction may be irrelevant to the album name. Artwork
    # needs either an explicit/unique release or identical art for EVERY option.
    try:
        hashes = {field_policy.normalized_value('cover', c['cover']) for c in covers}
    except media.MediaError:
        hashes = set()
    if (not release_ids or {c.get('release_id') for c in covers} != release_ids or len(hashes) != 1):
        covers = []
    return (sorted(albums, key=lambda c: c['id'])[0] if albums else None,
            sorted(covers, key=lambda c: c['id'])[0] if covers else None)


def active_tracks(conn, ignore_job_id=None):
    result = set()
    for row in conn.execute("SELECT id,payload FROM jobs WHERE state NOT IN ('succeeded','failed','cancelled','cancelled_partial','completed_with_errors')"):
        if row['id'] == ignore_job_id: continue
        for item in json.loads(row['payload']).get('items', []):
            if isinstance(item, dict) and item.get('track_id'): result.add(item['track_id'])
    return result


def evaluate(settings, conn, row, active, *, busy=None, retired=None):
    from . import automatic_quality, lyrics_review
    metadata = json.loads(row['metadata'])
    saved_analysis = conn.execute('SELECT * FROM analyses WHERE track_id=?', (row['id'],)).fetchone()
    analysis = json.loads(saved_analysis['payload']) if saved_analysis and saved_analysis['input_sha256'] == row['sha256'] else {}
    latest = conn.execute('SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
    pending = json.loads(latest['payload']) if latest and latest['input_sha256'] == row['sha256'] else {}
    actor = automatic_quality.actor_snapshot(conn, active)
    policy = field_policy.load(conn, row['id'])
    blocked = []
    if not isinstance(analysis, dict) or not isinstance(pending, dict):
        analysis, pending = {}, {}
        blocked.append('invalid_evidence')
    if not settings.writable: blocked.append('read_only')
    if not actor['write_authorized']: blocked.append('authorization_required')
    recovery = conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()
    if recovery and recovery['value'] == 'true': blocked.append('recovery_required')
    if (row['status'] in {'error', 'missing', 'conflict'} or metadata.get('input_error')
            or metadata.get('critical_conflicts') or not metadata.get('write_supported')):
        blocked.append('file_unavailable')
    retired = catalog_sources.completed_sources(conn) if retired is None else retired
    if (row['id'] in retired or metadata.get('format') == 'cue' or metadata.get('cue_album_image')
            or not enrichment_gaps.individual_music(metadata, row['sha256'])):
        blocked.append('excluded_recording')
    busy = active_tracks(conn) if busy is None else busy
    if row['id'] in busy: blocked.append('unresolved_task')
    if not analysis: blocked.append('analysis_stale' if saved_analysis else 'analysis_missing')
    candidates = analysis.get('candidates', [])
    segments = analysis.get('recognition_segments') or []
    if (not isinstance(candidates, list) or len(candidates) > 128
            or any(not isinstance(c, dict) or not isinstance(c.get('tags', {}), dict)
                   or not isinstance(c.get('match', {}), dict) or not isinstance(c.get('releases', []), list)
                   or any(not isinstance(r, dict) for r in c.get('releases', [])) for c in candidates)
             or not isinstance(segments, list) or any(not isinstance(s, dict)
                    or not isinstance(s.get('candidate') or {}, dict) for s in segments)):
        blocked.append('invalid_evidence')
        candidates = []
    audit = metadata.get('audit') or {}
    if (not isinstance(audit, dict) or audit.get('state') != 'passed' or audit.get('sha256') != row['sha256']
            or audit.get('profile') != 'full_decode_all_audio_streams'
            or not HASH.fullmatch(str(audit.get('audio_identity', '')))):
        blocked.append('audio_audit_required')
    if input_readiness.describe(metadata, grace_seconds=settings.input_grace_seconds)['state'] != 'ready':
        blocked.append('input_stabilizing')
    if analysis.get('possible_mix') or analysis.get('critical_conflicts') or analysis.get('unresolved_conflicts'):
        blocked.append('recording_conflict')
    if field_policy.stale_fields(settings, row, policy): blocked.append('field_lock_stale')
    matches, fingerprints, identity_error = recording(conn, analysis, metadata) if not blocked else ([], [], blocked[0])
    album, cover = album_and_cover(conn, metadata, candidates, matches) if matches else (None, None)
    tags = metadata.get('tags', {})
    chosen = {field: matches[0] if matches else None for field in ('title', 'artist')}
    chosen.update(album=album, cover=cover)
    payload = {'tags': {}, 'field_sources': {}, 'sources': [], 'source_priority_version': field_policy.sources(conn)['version']}
    changes, remaining = [], []
    previous = field_policy.payload_values(pending) if not blocked else {}
    album_unchanged = bool(scalar(tags, 'album').strip())
    for field in FIELDS:
        present = bool(metadata.get('has_art')) if field == 'cover' else bool(scalar(tags, field).strip())
        if present: continue
        c = chosen[field]
        reason = blocked[0] if blocked else identity_error
        if not reason and not c:
            reason = 'cover_edition_unconfirmed' if field == 'cover' else 'album_unconfirmed' if field == 'album' else 'candidate_unavailable'
        if not reason and field in policy['locks']: reason = 'field_locked'
        if not reason and not permitted(conn, c): reason = 'provider_usage_denied'
        if not reason:
            value = c['cover'] if field == 'cover' else scalar(c.get('tags', {}), field)
            if field in previous and field_policy.normalized_value(field, previous[field]) != field_policy.normalized_value(field, value):
                reason = 'pending_choice'
        if not reason and field == 'cover' and not album_unchanged and not any(change['field'] == 'album' for change in changes):
            reason = 'album_unconfirmed'
        if not reason and field == 'cover' and 'album' in previous:
            cover_album = scalar(tags, 'album') or scalar((album or {}).get('tags', {}), 'album')
            if field_policy.normalized_value('album', previous['album']) != field_policy.normalized_value('album', cover_album):
                reason = 'pending_choice'
        if not reason and field == 'cover':
            inventory = metadata.get('slot_inventory', {})
            if inventory.get('truncated') or not HASH.fullmatch(str(inventory.get('inventory_sha256', ''))):
                reason = 'cover_slot_unconfirmed'
            else:
                payload['slot_selection'] = {'inventory_sha256': inventory['inventory_sha256'], 'cover_slot_id': 'new'}
        if not reason:
            part = field_policy.selected_payload(settings, conn, row, [c], saved_analysis['updated_at'])
            values = set(field_policy.payload_values(part))
            if field not in values:
                reason = 'field_locked'
            else:
                if values != {field}: field_policy.exclude_selected_fields(part, values - {field})
                try:
                    part = provider_policy.require_payload(conn, row, part, 'embed')
                except media.MediaError:
                    reason = 'provider_usage_denied'
        if reason:
            remaining.append({'field': field, 'code': reason, 'message': MESSAGES[reason]})
            continue
        payload['tags'].update(part['tags'])
        if field == 'cover': payload['cover'] = part['cover']
        payload['field_sources'].update(part['field_sources'])
        payload['field_sources'][field]['reason'] = CONTRACT
        for source in part['sources']:
            if source not in payload['sources']: payload['sources'].append(source)
        changes.append({'field': field, 'before': False if field == 'cover' else '',
                        'after': True if field == 'cover' else value, 'source': c['provider']})
    if not metadata.get('lrc'):
        remaining.append({'field': 'synced_lyrics', 'code': 'lyrics_review_required', 'message': MESSAGES['lyrics_review_required']})
    usages = [c.get('usage') for c in fingerprints + matches + [c for c in (album, cover) if c]]
    rules = {(u['provider'], u['data_type']) for u in usages if isinstance(u, dict) and u.get('provider') and u.get('data_type')}
    document = conn.execute('SELECT value FROM settings WHERE key=?', (lyrics_document.key(row['id']),)).fetchone()
    review = lyrics_review.load(conn, row['id'])
    dependencies = {'contract': CONTRACT, 'file_revision': row['sha256'], 'metadata': digest(metadata),
                    'analysis': digest(dict(saved_analysis)) if saved_analysis else None,
                    'proposal': digest(dict(latest)) if latest else None, 'locks': policy,
                    'source_priorities': field_policy.sources(conn), 'actor': actor,
                    'document': digest(document['value']) if document else None, 'review': digest(review),
                    'rules': [provider_policy.rule(conn, p, k) for p, k in sorted(rules)],
                    'payload': digest(payload), 'busy': row['id'] in busy}
    accepted = lyrics_review.current_review(row, review)['accepted_current_revision']
    return {'track_id': row['id'], 'relative_path': row['relative_path'], 'title': scalar(tags, 'title'),
            'artist': scalar(tags, 'artist'), 'token': digest(dependencies), 'ready': bool(changes) and not blocked,
            'changes': changes, 'remaining': remaining, 'blocked': blocked,
            'publication': {'eligible': accepted, 'reason': '' if accepted else 'Для Ready сначала нужно принять профиль этой записи.'},
            '_payload': payload, '_proposal_id': latest['id'] if latest else None, '_review': review}


def public(result):
    return {key: value for key, value in result.items() if not key.startswith('_')}


def freeze(settings, conn, row, active, expected_token):
    result = evaluate(settings, conn, row, active)
    if not result['ready'] or result['token'] != expected_token:
        raise media.MediaError('confirmed_fields_stale', 'Подтверждённые поля изменились. Обновите план перед записью.')
    payload = field_policy.validate_payload(settings, conn, row, result['_payload'], 'apply')
    payload = provider_policy.require_payload(conn, row, payload, 'embed')
    return {'proposal_id': result['_proposal_id'], 'proposal': payload, 'acceptance_mode': MODE,
            'confirmed_fields': {'contract': CONTRACT, 'dependency_sha256': result['token'],
                                 'apply_payload_sha256': digest(payload), 'fields': [c['field'] for c in result['changes']],
                                 'review': result['_review']}}


def assert_frozen(settings, row, item, job):
    frozen = item.get('confirmed_fields') or {}
    payload = json.loads(job['payload'])
    active = {'auth_epoch': job['auth_epoch']}
    for source, target in (('auth_token_id', 'api_token_id'), ('auth_scopes', 'auth_scopes'), ('auth_limited', 'auth_limited')):
        if source in payload: active[target] = payload[source]
    with db.connect(settings.db_path) as conn:
        fresh = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (row['id'],)).fetchone()
        if not fresh: raise media.MediaError('confirmed_fields_stale', 'Файл недоступен')
        result = evaluate(settings, conn, fresh, active, busy=active_tracks(conn, job['id']))
    if (frozen.get('contract') != CONTRACT or not result['ready']
            or result['token'] != frozen.get('dependency_sha256')
            or digest(item.get('proposal', {})) != frozen.get('apply_payload_sha256')
            or result['_payload'].get('lrc') is not None or item.get('proposal', {}).get('lrc') is not None):
        raise media.MediaError('confirmed_fields_stale', 'Зависимости подтверждённых полей изменились до записи.')
    return result


def retained_draft(conn, current, metadata, item, result):
    """Keep unselected pending values as a draft of the unchanged audio packet set."""
    if current['sha256'] != item['sha256'] or not result.get('audio_identity'): return None
    before = json.loads(current['metadata'])
    if any((before.get(k) or '') != (metadata.get(k) or '') for k in ('lrc', 'lyrics')): return None
    latest = conn.execute('SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (current['id'],)).fetchone()
    if not latest or latest['id'] != item.get('proposal_id') or latest['input_sha256'] != item['sha256']: return None
    pending = json.loads(latest['payload'])
    applied = set(field_policy.payload_values(item['proposal']))
    values = set(field_policy.payload_values(pending)) - applied
    if not values: return None
    for field in applied:
        if field in {'cover', 'lrc'}: pending.pop(field, None)
        else: pending.get('tags', {}).pop(field, None)
        pending.get('field_sources', {}).pop(field, None)
    if 'cover' not in values: pending.pop('artwork_transform', None)
    if 'lrc' not in values:
        pending.pop('lyrics_document', None)
        pending.pop('candidate_lyric_adjustment', None)
    selected = pending.get('slot_selection') or {}
    selected = {k: v for k, v in selected.items() if k == 'cover_slot_id' and 'cover' in values or k == 'lyrics_slot_id' and 'lrc' in values}
    if selected:
        inventory = metadata.get('slot_inventory') or {}
        if inventory.get('truncated') or not inventory.get('inventory_sha256'): return None
        for field, slot in selected.items():
            kind = 'cover' if field == 'cover_slot_id' else 'lyrics'
            if slot != 'new' and not any(x['id'] == slot and x['kind'] == kind and x.get('editable') for x in inventory.get('slots', [])): return None
        pending['slot_selection'] = {**selected, 'inventory_sha256': inventory['inventory_sha256']}
    else: pending.pop('slot_selection', None)
    candidates = {v.get('candidate_id') for v in pending.get('field_sources', {}).values() if isinstance(v, dict)}
    pending['sources'] = [s for s in pending.get('sources', []) if s.get('id') in candidates]
    if any(not provider_policy._allowed(conn, u, 'cache') for u in provider_policy.references(pending)): return None
    import uuid
    identity = str(uuid.uuid4())
    conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (identity, current['id'], metadata['sha256'], db.encode(pending), db.utcnow()))
    return identity


def publication_followup(settings, conn, current, metadata, item, result, job):
    """Carry existing acceptance only across proven identical audio AND lyrics.

    No new word, timing, instrumental or listening decision is manufactured.
    The normal Ready worker still audits, checks grant, policy and destination.
    """
    from . import lyrics_review, publication
    params = json.loads(job['payload']).get('params', {})
    if not params.get('publish_confirmed'): return {'state': 'work_only'}
    import uuid
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, CONTRACT + ':' + job['id'] + ':' + current['id'] + ':' + metadata['sha256']))
    existing = conn.execute('SELECT kind,state,payload,auth_epoch FROM jobs WHERE id=?', (identity,)).fetchone()
    if existing:
        previous = json.loads(existing['payload'])
        if (existing['kind'] != 'publish' or existing['auth_epoch'] != job['auth_epoch']
                or previous.get('confirmed_fields_parent') != job['id']
                or previous.get('items', [{}])[0].get('sha256') != metadata['sha256']):
            raise media.MediaError('confirmed_fields_followup_conflict', 'Изменился сохранённый план публикации')
        return {'state': existing['state'], 'job_id': identity}
    old = json.loads(current['metadata'])
    saved = lyrics_review.load(conn, current['id'])
    frozen = item['confirmed_fields'].get('review')
    old_review = lyrics_review.current_review(current, saved)
    if (digest(saved) != digest(frozen) or not old_review['accepted_current_revision']
            or saved.get('policy_version') != lyrics_review.POLICY_VERSION
            or not result.get('audio_identity') or old.get('audit', {}).get('audio_identity') != result['audio_identity']
            or metadata.get('audit', {}).get('audio_identity') != result['audio_identity']
            or any((old.get(k) or '') != (metadata.get(k) or '') for k in ('lrc', 'lyrics'))):
        return {'state': 'needs_review', 'reason': 'previous_acceptance_or_unchanged_audio_required'}
    new_row = dict(current, sha256=metadata['sha256'], metadata=db.encode(metadata))
    document = lyrics_review.document(new_row)
    if (document['words_revision'] != old_review['words_revision']
            or document['timings_revision'] != old_review['timings_revision']):
        return {'state': 'needs_review', 'reason': 'lyrics_changed'}
    carried = {**saved, 'version': saved['version'] + 1, 'file_revision': metadata['sha256'],
               'document_revision': document['document_revision'], 'accepted_revision': metadata['sha256'],
               'acceptance_origin': CONTRACT, 'carried_from_revision': current['sha256'],
               'updated_at': db.utcnow()}
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('lyrics_review:' + current['id'], db.encode(carried)))
    gate = lyrics_review.publication_gate(settings, new_row, conn=conn)
    if gate['state'] != 'passed': return {'state': 'needs_review', 'reason': ','.join(gate['reasons'])}
    publish_item = {'track_id': current['id'], 'relative_path': current['relative_path'], 'sha256': metadata['sha256'],
                    'publication_review': lyrics_review.freeze_publication(settings, new_row, conn=conn),
                    'publication_target': publication.freeze_target(new_row)}
    parent = json.loads(job['payload'])
    payload = {'items': [publish_item], 'params': {}, 'confirmed_fields_parent': job['id']}
    for key in ('auth_token_id', 'auth_scopes', 'auth_limited', '_trace'):
        if key in parent: payload[key] = parent[key]
    stamp = db.utcnow()
    conn.execute("INSERT OR IGNORE INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,'publish','queued','pending',?,1,?,?,?)",
                 (identity, db.encode(payload), stamp, stamp, job['auth_epoch']))
    db.event(conn, 'job_created', {'job_id': identity, 'kind': 'publish', 'parent_job_id': job['id']})
    return {'state': 'queued', 'job_id': identity}


def install(app, settings):
    @app.get('/api/confirmed-fields/plan')
    def plan(request: Request, after: str = Query('', max_length=36), limit: int = Query(30, ge=1, le=50)):
        active = security.require_session(request, settings, owner_only=True)
        if after and not UUID.fullmatch(after): raise security.error(422, 'invalid_cursor', 'Обновите план коллекции')
        with db.connect(settings.db_path) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            rows = conn.execute('SELECT * FROM tracks WHERE is_current=1 AND id>? ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
            total = conn.execute('SELECT COUNT(*) FROM tracks WHERE is_current=1').fetchone()[0]
            busy = active_tracks(conn)
            retired = catalog_sources.completed_sources(conn)
            items = [public(evaluate(settings, conn, row, active, busy=busy, retired=retired)) for row in rows[:limit]]
        return {'contract': CONTRACT, 'items': items, 'next_after': rows[limit - 1]['id'] if len(rows) > limit else None,
                'scanned': len(items), 'total': total}
