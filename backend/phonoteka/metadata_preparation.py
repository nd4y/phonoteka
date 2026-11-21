"""Explicit bounded evidence preparation. No audio writes or automatic acceptance."""
import json
import uuid
from typing import Annotated

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from . import acoustid, artwork_preparation, confirmed_fields as fields, db, enrichment_gaps, field_policy, input_readiness, lyrics_review, media, provider_attempts, provider_policy, providers, recognition, security, webhooks
from .providers import LYRICS_QUERY_PROFILE, normalized, scalar

KEY = 'metadata_preparation'
CONTRACT = 'metadata-preparation-v1'
LIMIT = 50
STAGES = ('scan_first', 'scan_second', 'search')
WAITING = {'queued', 'running', 'paused', 'waiting_authorization', 'waiting', 'deferred', 'waiting_provider', 'waiting_storage'}
SAFE_BLOCKS = {'input_stabilizing', 'analysis_missing', 'analysis_stale', 'audio_audit_required'}
CACHE_FALLBACK_PROFILE = 'exhausted-acoustid-cache-v1'
UNSEARCHED_RECOVERY_PROFILE = 'unsearched-preparation-stale-v1'
LYRICS_REFRESH_PROFILE = 'lyric-only-expired-search-v1'
ARTWORK_PROFILE = 'unqueried-known-album-artwork-v1'
ACOUSTID_PROFILE = 'previously-unconfigured-acoustid-v1'
RECORDING_REFERENCE_PROFILE = 'embedded-recording-reference-v1'


def load(conn, identity=None):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (KEY if identity is None else KEY + ':' + identity,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(conn, run):
    for key in (KEY, KEY + ':' + run['id']):
        conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, db.encode(run)))


def guards(conn, row):
    p = conn.execute('SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
    meta = json.loads(row['metadata'])
    decision = meta.get('recording_classification') or {}
    return fields.digest([field_policy.load(conn, row['id']), dict(p) if p else None,
                          decision if decision.get('origin') == 'owner' else None])


def unsearched_recovery(settings, conn, row, active, signature, receipt, kind, current_guard):
    """One new capability, never a replay or removal of the original receipt."""
    from .worker import root_identity
    try:
        binding = json.loads(receipt)
        if not isinstance(binding, dict) or set(binding) != {'run_id'}:
            return None
        identity = binding['run_id']
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
            return None
        run = load(conn, identity)
        stage = run.get('stage')
        if (run.get('id') != identity or run.get('state') != 'stopped' or run.get('reason') != 'preparation_stale'
                or stage not in STAGES[:2] or run.get('kind') != kind):
            return None
        completed = [] if stage == 'scan_first' else ['scan_first']
        items = run.get('items')
        if (run.get('completed_stages') != completed or not isinstance(items, list) or not 1 <= len(items) <= LIMIT
                or type(run.get('total')) is not int or run['total'] != len(items)):
            return None
        keys = {'track_id', 'sha256', 'relative_path', 'scan', 'guards'}
        if any(not isinstance(i, dict) or set(i) != keys or type(i['scan']) is not bool
               or not isinstance(i['track_id'], str) or str(uuid.UUID(i['track_id'])) != i['track_id']
               or not isinstance(i['relative_path'], str) or not i['relative_path']
               or not isinstance(i['sha256'], str) or not fields.HASH.fullmatch(i['sha256'])
               or not isinstance(i['guards'], str) or not fields.HASH.fullmatch(i['guards']) for i in items):
            return None
        ids = {i['track_id'] for i in items}
        tokens = run.get('tokens')
        if (len(ids) != len(items) or len({i['relative_path'] for i in items}) != len(items)
                or not isinstance(tokens, dict) or set(tokens) != ids
                or any(not isinstance(t, str) or not fields.HASH.fullmatch(t) for t in tokens.values())):
            return None
        original = next((i for i in items if i['track_id'] == row['id']), None)
        if (not original or original['sha256'] != row['sha256'] or original['relative_path'] != row['relative_path']
                or original['guards'] != current_guard):
            return None
        dependency = [CONTRACT, row['id'], row['sha256'], kind, original['guards']]
        if signature not in {fields.digest(dependency), fields.digest(dependency + [CACHE_FALLBACK_PROFILE])}:
            return None
        epoch = conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone()
        recovery = conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()
        actual_root = root_identity(settings.work_root)
        stored_root = conn.execute("SELECT value FROM settings WHERE key='work_root_identity'").fetchone()
        if (not epoch or type(active.get('auth_epoch')) is not int or type(run.get('epoch')) is not int
                or run['epoch'] != active['auth_epoch'] or run['epoch'] != int(epoch[0])
                or recovery and recovery[0] != 'false' or not stored_root
                or run.get('root') != actual_root or json.loads(stored_root[0]) != actual_root):
            return None
        stage_ids = {s: str(uuid.uuid5(uuid.UUID(identity), s)) for s in STAGES}
        if run.get('job_id') != stage_ids[stage] or run.get('last_job_id') != (stage_ids['scan_first'] if completed else None):
            return None
        scanned = [i for i in items if i['scan']]
        if not scanned:
            return None
        for name in [*completed, stage]:
            job = conn.execute('SELECT * FROM jobs WHERE id=?', (stage_ids[name],)).fetchone()
            if (not job or job['kind'] != 'scan' or job['auth_epoch'] != run['epoch'] or job['cancel_requested']
                    or job['state'] not in {'succeeded', 'failed', 'completed_with_errors'}
                    or job['error'] not in {None, 'preparation_stale'} or job['total'] != len(scanned)):
                return None
            payload = json.loads(job['payload'])
            if payload.get('trigger') != CONTRACT or payload.get('metadata_preparation_id') != identity or payload.get('items') != scanned:
                return None
            results = conn.execute('SELECT * FROM job_items WHERE job_id=?', (job['id'],)).fetchall()
            expected = {i['relative_path']: i for i in scanned}
            for result in results:
                value = json.loads(result['result'])
                item = expected.get(result['item_key'])
                if not item or not isinstance(value, dict) or result['state'] not in {'succeeded', 'failed'}:
                    return None
                if result['state'] == 'failed' and value.get('code') != 'preparation_stale':
                    return None
                if result['state'] == 'succeeded' and (value.get('track_id') != item['track_id'] or value.get('sha256') != item['sha256']):
                    return None
            if (job['completed'] != sum(r['state'] == 'succeeded' for r in results)
                    or job['failed'] != sum(r['state'] == 'failed' for r in results)):
                return None
            if name in completed:
                proof = conn.execute('SELECT value FROM settings WHERE key=?', ('metadata_scan:' + job['id'],)).fetchone()
                if (job['state'] != 'succeeded' or job['completed'] != len(scanned) or job['failed']
                        or not proof or json.loads(proof[0]).get('complete') is not True):
                    return None
        search = stage_ids['search']
        if (conn.execute("SELECT 1 FROM jobs WHERE id=? OR CASE WHEN json_valid(payload) THEN json_extract(payload,'$.metadata_preparation_id')=? AND kind IN ('analyze','recognize') ELSE 0 END LIMIT 1", (search, identity)).fetchone()
                or conn.execute('SELECT 1 FROM job_items WHERE job_id=? LIMIT 1', (search,)).fetchone()):
            return None
        batches = (identity, *stage_ids.values())
        if conn.execute("SELECT 1 FROM settings WHERE key GLOB ? AND CASE WHEN json_valid(value) THEN json_type(value)<>'object' OR json_extract(value,'$.batch_id') IN (?,?,?,?) ELSE 1 END LIMIT 1",
                        (provider_attempts.PREFIX + '*', *batches)).fetchone():
            return None
        recovered = fields.digest([UNSEARCHED_RECOVERY_PROFILE, signature])
        if conn.execute('SELECT 1 FROM settings WHERE key=?', ('metadata_prepared:' + recovered,)).fetchone():
            return None
        return recovered
    except (ValueError, TypeError, KeyError, AttributeError, OSError, media.MediaError):
        return None


def lyric_refresh(conn, settings, row, metadata):
    """Refresh missing lyric evidence, never infer words or accept an instrumental."""
    if enrichment_gaps.missing_fields(metadata) != ['synced_lyrics']:
        return False
    if metadata.get('lyrics') or 'lrc' in field_policy.load(conn, row['id'])['locks']:
        return False
    if lyrics_review.load(conn, row['id']).get('instrumental_confirmed'):
        return False
    rule = provider_policy.rule(conn, 'lrclib', 'lyrics')
    if not all(rule.get('allow_' + operation) for operation in ('fetch', 'cache', 'embed')):
        return False
    proposal = conn.execute('SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
    if proposal and proposal['input_sha256'] == row['sha256']:
        draft = json.loads(proposal['payload'])
        if draft.get('lrc') or (draft.get('lyrics_document') or {}).get('lines'):
            return False
    analysis = enrichment_gaps.project(conn, [row['id']]).get(row['id'])
    if analysis and any(not isinstance(source, dict) or source.get('provider') == 'lrclib' and
                        source.get('state') in enrichment_gaps.RETRY_STATES | enrichment_gaps.CONFIG_STATES | {'unknown_outcome'}
                        for source in analysis['sources']):
        return False
    # Only the lyric field is missing, so no AcoustID connection is consulted.
    reasons = enrichment_gaps.describe(conn, settings, row, analysis=analysis, proposal=proposal, connection={})
    return len(reasons) == 1 and reasons[0]['code'] in {'results_expired', 'search_stale'}


def lyric_variant(conn, row, metadata, analysis):
    """Offer a distinct query after a determinate, current legacy search."""
    if not analysis or metadata.get('lrc') or 'lrc' in field_policy.load(conn, row['id'])['locks']:
        return False
    tags = metadata.get('tags', {})
    query = analysis.get('query') or {}
    if not all(normalized(scalar(tags, field)) and
               normalized(scalar(tags, field)) == normalized(scalar(query, field))
               for field in ('title', 'artist')):
        return False
    duration = metadata.get('duration')
    if not fields.number(duration) or not 1 <= duration <= 3600:
        return False
    sources = [s for s in analysis.get('sources', []) if isinstance(s, dict) and s.get('provider') == 'lrclib']
    if (len(sources) != 1 or sources[0].get('state') not in {'no_match', 'candidates'}
            or sources[0].get('query_profile') == LYRICS_QUERY_PROFILE
            or any(c.get('lrc') for c in analysis.get('candidates', []))):
        return False
    if enrichment_gaps.project(conn, [row['id']]).get(row['id'], {}).get('last_failed_search'):
        return False
    if lyrics_review.load(conn, row['id']).get('instrumental_confirmed'):
        return False
    rule = provider_policy.rule(conn, 'lrclib', 'lyrics')
    if not all(rule.get('allow_' + operation) for operation in ('fetch', 'cache', 'embed')):
        return False
    proposal = conn.execute('SELECT input_sha256,payload FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                            (row['id'],)).fetchone()
    if not proposal or proposal['input_sha256'] != row['sha256']:
        return True
    draft = json.loads(proposal['payload'])
    return not (draft.get('lrc') or (draft.get('lyrics_document') or {}).get('lines'))


def acoustid_connection(settings):
    if not settings.external_fingerprints:
        return {}
    try:
        return acoustid.public_connection(settings)
    except (OSError, ValueError, media.MediaError):
        return {}


def acoustid_continuation(settings, conn, row, metadata, analysis, result, connection):
    """Return whether the legacy result is handled by this preparation path."""
    audit = metadata.get('audit') or {}
    identity = audit.get('audio_identity')
    if (not settings.external_fingerprints or not settings.external_metadata
            or audit.get('sha256') != row['sha256'] or audit.get('state') != 'passed'
            or audit.get('profile') != 'full_decode_all_audio_streams'
            or not fields.HASH.fullmatch(str(identity or ''))):
        return False, None
    duration = metadata.get('duration')
    if not fields.number(duration) or not 3 <= duration <= 21600:
        return False, None
    prior = recognition.pending_acoustid(analysis, row['sha256'], duration)
    if not prior:
        return False, None
    # Tag edits, settings churn and key rotation do not mint a second receipt.
    signature = fields.digest([CONTRACT, row['id'], identity, ACOUSTID_PROFILE])
    if conn.execute('SELECT 1 FROM settings WHERE key=?', ('metadata_prepared:' + signature,)).fetchone():
        return True, None
    if not provider_policy.rule(conn, 'acoustid', 'metadata').get('allow_fetch'):
        return False, None
    if connection is None:
        connection = acoustid_connection(settings)
    if not connection.get('configured'):
        return False, None
    usage = conn.execute('SELECT value FROM settings WHERE key=?',
                         (provider_attempts.RECOGNITION_PREFIX + identity,)).fetchone()
    remaining = provider_attempts.RECOGNITION_CALL_LIMIT - (json.loads(usage[0]).get('calls', 0) if usage else 0)
    if remaining <= 0:
        return False, None
    if enrichment_gaps.project(conn, [row['id']]).get(row['id'], {}).get('last_failed_search'):
        return True, None
    binding = {'connection_version': connection['version'], 'proof': fields.digest(prior)}
    return True, {'token': fields.digest([signature, result['token'], remaining, binding]), 'receipt': signature,
            'kind': 'recognize', 'fingerprint_requests_remaining': remaining,
            'scan': input_readiness.describe(metadata, grace_seconds=settings.input_grace_seconds)['state'] != 'ready',
            'acoustid_continuation': binding,
            'reason': 'AcoustID теперь подключён. Продолжим сохранённую проверку одним запросом AcoustID, без повторной отправки в Shazam.'}


def embedded_recording_check(settings, conn, row, metadata, analysis, result):
    if not settings.external_metadata:
        return False, None
    reference = providers.embedded_recording_id(metadata)
    audit = metadata.get('audit') or {}
    identity = audit.get('audio_identity')
    if (not reference or audit.get('sha256') != row['sha256'] or audit.get('state') != 'passed'
            or audit.get('profile') != 'full_decode_all_audio_streams'
            or not fields.HASH.fullmatch(str(identity or ''))):
        return False, None
    usage = conn.execute('SELECT value FROM settings WHERE key=?',
                         (provider_attempts.RECOGNITION_PREFIX + identity,)).fetchone()
    if not usage or json.loads(usage[0]).get('calls', 0) < provider_attempts.RECOGNITION_CALL_LIMIT:
        return False, None
    if any(c.get('provider') == 'musicbrainz' and c.get('recording_id') == reference
           and fields.permitted(conn, c, ('cache', 'derived_analysis')) for c in analysis.get('candidates', [])):
        return False, None
    signature = fields.digest([CONTRACT, row['id'], identity, RECORDING_REFERENCE_PROFILE, reference])
    previous = conn.execute('SELECT 1 FROM settings WHERE key=?', ('metadata_prepared:' + signature,)).fetchone()
    if previous:
        return True, None
    rule = provider_policy.rule(conn, 'musicbrainz', 'metadata')
    if (not all(rule.get('allow_' + operation) for operation in ('fetch', 'cache', 'derived_analysis'))
            or enrichment_gaps.project(conn, [row['id']]).get(row['id'], {}).get('last_failed_search')):
        return False, None
    if any(s.get('provider') == 'musicbrainz' and s.get('state') == 'unknown_outcome' for s in analysis.get('sources', [])):
        return True, None
    return True, {'token': fields.digest([signature, result['token']]), 'receipt': signature, 'kind': 'analyze',
                  'scan': input_readiness.describe(metadata, grace_seconds=settings.input_grace_seconds)['state'] != 'ready',
                  'embedded_recording_check': reference,
                  'reason': 'Сверим сохранённый идентификатор с MusicBrainz. Аудиоотпечаток не отправляется, результат останется на проверке.'}


def candidate(settings, conn, row, active, result, connection=None):
    if result['ready'] or set(result['blocked']) - SAFE_BLOCKS:
        return None
    gaps = [g for g in result['remaining'] if g['field'] in fields.FIELDS]
    if any(g['code'] in {'recording_conflict', 'recording_incomplete', 'field_locked', 'pending_choice', 'field_lock_stale'} for g in gaps):
        return None
    metadata = json.loads(row['metadata'])
    refresh_lyrics = not gaps and lyric_refresh(conn, settings, row, metadata)
    if not gaps and not refresh_lyrics and metadata.get('lrc'):
        return None
    saved = conn.execute('SELECT * FROM analyses WHERE track_id=?', (row['id'],)).fetchone()
    analysis = json.loads(saved['payload']) if saved and saved['input_sha256'] == row['sha256'] else {}
    if analysis.get('possible_mix') or analysis.get('critical_conflicts') or analysis.get('unresolved_conflicts'):
        return None
    matches, fingerprints, error = fields.recording(conn, analysis, metadata)
    if error in {'recording_conflict', 'recording_incomplete'}:
        return None
    legacy_lyrics = not refresh_lyrics and lyric_variant(conn, row, metadata, analysis)
    art_recordings = matches
    if not art_recordings and any(g['field'] == 'cover' for g in gaps):
        art_recordings = fields.tagged_artwork_recordings(conn, metadata, analysis)
    art_hint = None
    named_album = artwork_preparation.named_album_lookup(metadata, analysis)
    if (art_recordings or named_album) and any(g['field'] == 'cover' for g in gaps):
        rule = provider_policy.rule(conn, 'coverartarchive', 'cover')
        catalog_rule = provider_policy.rule(conn, 'musicbrainz', 'metadata')
        pending = conn.execute('SELECT input_sha256,payload FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'],)).fetchone()
        proposal = json.loads(pending['payload']) if pending and pending['input_sha256'] == row['sha256'] else {}
        edition_pending = (proposal.get('cover') or proposal.get('tags', {}).get('album')
                           or proposal.get('tags', {}).get('musicbrainz_albumid'))
        if (not edition_pending and 'cover' not in field_policy.load(conn, row['id'])['locks']
                and all(rule.get('allow_' + operation) for operation in ('fetch', 'cache', 'embed'))
                and all(catalog_rule.get('allow_' + operation) for operation in ('fetch', 'cache'))):
            # The original generic lookup may have had no album. A distinct,
            # album-scoped lookup adds artwork evidence without identifying the
            # audio again or selecting a release. A newer failed search must
            # be examined rather than replayed through this new capability.
            projected = enrichment_gaps.project(conn, [row['id']]).get(row['id'], {})
            if not projected.get('last_failed_search'):
                art_hint = artwork_preparation.album_hint(metadata, analysis, art_recordings) or named_album
    if not fingerprints and not art_hint:
        handled, continuation = acoustid_continuation(settings, conn, row, metadata, analysis, result, connection)
        if handled and (continuation or not legacy_lyrics):
            return continuation
        handled, reference_check = embedded_recording_check(settings, conn, row, metadata, analysis, result)
        if handled and (reference_check or not legacy_lyrics):
            return reference_check
    expanded_lyrics = not art_hint and legacy_lyrics
    if not gaps and not refresh_lyrics and not expanded_lyrics:
        return None
    kind = 'analyze' if refresh_lyrics or expanded_lyrics or art_hint or matches or fingerprints else 'recognize'
    if not settings.external_metadata or kind == 'recognize' and not settings.external_fingerprints:
        return None
    identity = (metadata.get('audit') or {}).get('audio_identity')
    usage = conn.execute('SELECT value FROM settings WHERE key=?', (provider_attempts.RECOGNITION_PREFIX + str(identity),)).fetchone()
    calls = json.loads(usage[0]).get('calls', 0) if usage else 0
    remaining = max(0, provider_attempts.RECOGNITION_CALL_LIMIT - calls) if identity else None
    # Preserve old receipts. Only the newly supported exhausted-cache path gets
    # one capability-specific evaluation; it cannot reserve or send AcoustID.
    # Failed/cancelled receipts for this profile still prevent automatic replay.
    dependency = [CONTRACT, row['id'], row['sha256'], kind, guards(conn, row)]
    if art_hint:
        dependency.append([ARTWORK_PROFILE, fields.digest(art_hint)])
    if expanded_lyrics:
        dependency.append(LYRICS_QUERY_PROFILE)
    if refresh_lyrics:
        # A new capability is not permission to replay a previous attempt.
        # Keep every old receipt, including failed, cancelled and unknown runs.
        previous = [dependency, [*dependency[:3], 'recognize', dependency[4]]]
        previous.append(previous[1] + [CACHE_FALLBACK_PROFILE])
        if any(conn.execute('SELECT 1 FROM settings WHERE key=?', ('metadata_prepared:' + fields.digest(value),)).fetchone() for value in previous):
            return None
        dependency.append(LYRICS_REFRESH_PROFILE)
    if kind == 'recognize' and remaining == 0:
        dependency.append(CACHE_FALLBACK_PROFILE)
    signature = fields.digest(dependency)
    receipt = conn.execute('SELECT value FROM settings WHERE key=?', ('metadata_prepared:' + signature,)).fetchone()
    recovery_of = None
    if receipt:
        if refresh_lyrics or expanded_lyrics or art_hint:
            return None
        recovered = unsearched_recovery(settings, conn, row, active, signature, receipt[0], kind, dependency[4])
        if not recovered:
            return None
        recovery_of, signature = signature, recovered
    stable = input_readiness.describe(metadata, grace_seconds=settings.input_grace_seconds)['state'] == 'ready'
    return {'token': fields.digest([signature, result['token'], remaining]), 'receipt': signature,
            'fingerprint_requests_remaining': remaining if kind == 'recognize' else None,
            'kind': kind, 'scan': bool(recovery_of) or not stable,
            **({'lyric_search_only': True} if refresh_lyrics or expanded_lyrics else {}),
            **({'expanded_lyrics': True} if expanded_lyrics else {}),
            **({'album_hint': art_hint} if art_hint else {}),
            **({'recovery_of': recovery_of} if recovery_of else {}),
            'reason': 'Запросим обложки известного альбома. Аудиоотпечаток, текст и файлы не меняются; выбор издания останется на проверке.' if art_hint else
                      'Поищем текст по названию и исполнителю другим запросом LRCLIB. Новые таймкоды останутся на проверке.' if expanded_lyrics else
                      'Обновим устаревший поиск текста. Распознавание по отпечатку не запускается; текст останется на проверке.' if refresh_lyrics else
                      'Предыдущая подготовка остановилась до поиска. Заново проверим файлы и доступные данные.' if recovery_of else
                      'Проверим сохранённые отпечатки. Новые запросы распознавания недоступны.' if kind == 'recognize' and remaining == 0 else
                      'Проверим стабильность и найдём недостающие данные.' if not stable else
                      'Повторим поиск по аудио.' if kind == 'recognize' else 'Уточним данные музыкальных источников.'}


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tokens: dict[Annotated[str, Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')],
                 Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]] = Field(min_length=1, max_length=LIMIT)


class Stop(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Annotated[str, Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')]


def public(run, conn):
    result = {k: run[k] for k in ('id', 'state', 'stage', 'job_id', 'total', 'completed_stages', 'reason', 'started_at', 'finished_at') if k in run}
    job_id = run.get('job_id') or run.get('last_job_id')
    job = conn.execute('SELECT state,total,completed,failed FROM jobs WHERE id=?', (job_id,)).fetchone()
    if job:
        result['progress'] = dict(job)
        result['progress']['skipped'] = conn.execute("SELECT count(*) FROM job_items WHERE job_id=? AND json_extract(result,'$.skipped')=1", (job_id,)).fetchone()[0]
    return result


def validate(conn, settings, run, job=None):
    from .worker import root_identity
    if run.get('collection_fill_id'):
        from . import preparation
        parent = preparation.load(conn)
        if parent.get('id') != run['collection_fill_id'] or parent.get('state') != 'active' or parent.get('batch_id') != run['id']:
            raise media.MediaError('preparation_stopped', 'Заполнение папки остановлено')
    if run.get('state') != 'active':
        raise media.MediaError('preparation_stopped', 'Подготовка остановлена')
    epoch = int(conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone()[0])
    recovery = conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()
    if epoch != run['epoch'] or recovery and recovery[0] == 'true':
        raise media.MediaError('authorization_changed', 'Изменились разрешения или требуется восстановление')
    if root_identity(settings.work_root) != run['root']:
        raise media.MediaError('root_identity_changed', 'Изменилось подключение рабочей папки')
    stored = conn.execute("SELECT value FROM settings WHERE key='work_root_identity'").fetchone()
    if stored and json.loads(stored[0]) != run['root']:
        raise media.MediaError('root_identity_changed', 'Рабочая папка требует сверки после переподключения')
    if job:
        payload = json.loads(job['payload'])
        expected = str(uuid.uuid5(uuid.UUID(run['id']), run['stage']))
        if (job['id'] != run.get('job_id') or job['id'] != expected or job['auth_epoch'] != run['epoch']
                or payload.get('items') != stage_items(run) or job['kind'] != stage_kind(run)):
            raise media.MediaError('preparation_invalid', 'Задача не соответствует сохранённой подготовке')
    busy = fields.active_tracks(conn, run.get('job_id'))
    for item in run['items']:
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],)).fetchone()
        if (not row or row['sha256'] != item['sha256'] or row['relative_path'] != item['relative_path']
                or guards(conn, row) != item['guards'] or item['track_id'] in busy):
            raise media.MediaError('preparation_stale', 'Запись изменилась или занята другой задачей. Обновите план.')


def stage_items(run):
    return [i for i in run['items'] if i['scan']] if run['stage'].startswith('scan_') else run['items']


def stage_kind(run):
    return 'scan' if run['stage'].startswith('scan_') else run['kind']


def advance(conn, settings, run):
    if run.get('state') != 'active':
        return
    try:
        validate(conn, settings, run)
        if run.get('job_id'):
            job = conn.execute('SELECT * FROM jobs WHERE id=?', (run['job_id'],)).fetchone()
            if job and job['state'] in WAITING:
                return
            if not job or job['state'] != 'succeeded':
                raise media.MediaError('preparation_child_stopped', 'Этап остановлен: ' + (job['state'] if job else 'задача недоступна'))
            if run['stage'].startswith('scan_'):
                receipt = conn.execute('SELECT value FROM settings WHERE key=?', ('metadata_scan:' + job['id'],)).fetchone()
                if not receipt or json.loads(receipt[0]).get('complete') is not True:
                    raise media.MediaError('preparation_scan_incomplete', 'Проверка стабильности не завершена')
            run['completed_stages'].append(run['stage'])
            run['last_job_id'] = run['job_id']
            run['job_id'] = None
        for stage in STAGES:
            if stage in run['completed_stages']:
                continue
            run['stage'] = stage
            items = stage_items(run)
            if not items:
                run['completed_stages'].append(stage)
                continue
            if stage == 'search':
                for item in items:
                    row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (item['track_id'],)).fetchone()
                    if input_readiness.describe(json.loads(row[0]), grace_seconds=settings.input_grace_seconds)['state'] != 'ready':
                        run['reason'] = 'waiting_input_grace'
                        return
                if not settings.external_metadata or run['kind'] == 'recognize' and not settings.external_fingerprints:
                    raise media.MediaError('provider_disabled', 'Источники отключены')
            if not webhooks.can_accept(conn):
                run['reason'] = 'webhook_queue_full'
                return
            identity = str(uuid.uuid5(uuid.UUID(run['id']), stage))
            payload = {'trigger': CONTRACT, 'metadata_preparation_id': run['id'], 'items': items, 'params': {}}
            stamp = db.utcnow()
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,?,'queued','pending',?,?,?,?,?)",
                         (identity, stage_kind(run), db.encode(payload), len(items), stamp, stamp, run['epoch']))
            db.event(conn, 'job_created', {'job_id': identity, 'kind': stage_kind(run), 'trigger': CONTRACT})
            run.update(job_id=identity, reason=None)
            return
        run.update(state='review', reason=None, finished_at=db.utcnow())
    except media.MediaError as exc:
        run.update(state='stopped', reason=exc.code, finished_at=db.utcnow())


def tick(settings):
    with db.transaction(settings.db_path) as conn:
        run = load(conn)
        if run.get('state') == 'active':
            advance(conn, settings, run)
            save(conn, run)


def authorize(settings, job):
    if CONTRACT not in job['payload'] and 'metadata_preparation_id' not in job['payload']:
        return
    payload = json.loads(job['payload'])
    if payload.get('trigger') != CONTRACT:
        if payload.get('metadata_preparation_id'):
            raise media.MediaError('preparation_invalid', 'Некорректная задача подготовки')
        return
    with db.connect(settings.db_path) as conn:
        run = load(conn, payload.get('metadata_preparation_id', ''))
        validate(conn, settings, run, job)


def _budget_result():
    return {'state': 'needs_review', 'skipped': True, 'code': 'recognition_attempt_budget_exhausted',
            'message': 'Нового подтверждения нет: бюджет распознавания исчерпан. Доступные результаты сохранены; лимит не сбрасывался.'}


def _retry_identity(worker, job, item):
    worker.authorize_grant(job)
    row = db.fetchone(worker.settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],))
    if not row or row['sha256'] != item['sha256']:
        raise media.MediaError('conflict', 'Версия файла изменилась после поиска')
    audit = json.loads(row['metadata']).get('audit')
    if (not isinstance(audit, dict) or audit.get('state') != 'passed' or audit.get('sha256') != item['sha256']
            or audit.get('profile') != 'full_decode_all_audio_streams'
            or not fields.HASH.fullmatch(str(audit.get('audio_identity', '')))):
        raise media.MediaError('audit_failed', 'Для ожидания распознавания нужна актуальная полная проверка аудио')
    return audit['audio_identity']


async def search(worker, job, item):
    try:
        if item.get('acoustid_continuation'):
            payload = json.loads(job['payload'])
            if (job['kind'] != 'recognize' or payload.get('trigger') != CONTRACT
                    or not payload.get('metadata_preparation_id') or item not in payload.get('items', [])):
                raise media.MediaError('preparation_invalid', 'AcoustID не соответствует сохранённой подготовке')
        if item.get('embedded_recording_check'):
            payload = json.loads(job['payload'])
            if (job['kind'] != 'analyze' or payload.get('trigger') != CONTRACT
                    or not payload.get('metadata_preparation_id') or item not in payload.get('items', [])):
                raise media.MediaError('preparation_invalid', 'Ссылка MusicBrainz не соответствует сохранённой подготовке')
            await worker.io(authorize, worker.settings, job)
        if item.get('lyric_search_only') is True:
            # Only a server-frozen preparation manifest can narrow the query.
            # Old unflagged runs keep their original provider scope.
            payload = json.loads(job['payload'])
            if (job['kind'] != 'analyze' or payload.get('trigger') != CONTRACT
                    or not payload.get('metadata_preparation_id') or item not in payload.get('items', [])):
                raise media.MediaError('preparation_invalid', 'Текст не соответствует сохранённой подготовке')
            await worker.io(authorize, worker.settings, job)
            result = await worker.analyze_item(job, item, lyrics_only=True)
        else:
            result = await worker.analyze_item(job, item)
    except media.MediaError as exc:
        if exc.code != 'recognition_attempt_budget_exhausted':
            raise
        # Native recognition already enforced its cache and durable allowance.
        return _budget_result()
    if job['kind'] == 'recognize' and result.get('retry_at'):
        identity = await worker.io(_retry_identity, worker, job, item)
        exhausted = False
        try:
            await worker.io(provider_attempts.require_recognition_capacity, worker.settings, identity)
        except provider_attempts.UsageError as exc:
            if exc.code != 'recognition_attempt_budget_exhausted':
                raise
            exhausted = True
        if await worker.io(_retry_identity, worker, job, item) != identity:
            raise media.MediaError('conflict', 'Аудиоревизия изменилась после поиска')
        if exhausted:
            # A fallback may have spent the last allowance after another
            # fingerprint source requested a retry. Preserve its saved analysis,
            # but do not park a retry that has no remaining send capacity.
            return _budget_result()
    return result


async def scan(worker, job):
    """Observe frozen existing files only; never mark others missing or relink CUE."""
    authorize(worker.settings, job)
    items = json.loads(job['payload'])['items']
    async def one(item):
        if worker.cancelled(job['id']):
            return
        try:
            worker.authorize_grant(job)
            result = await worker.io(worker.scan_one, item['relative_path'], job['id'], item)
            worker.item_result(job['id'], item['relative_path'], 'failed' if result.get('input_error') else 'succeeded', result)
        except Exception as exc:
            worker.item_result(job['id'], item['relative_path'], 'failed', worker.failure(exc))
    await worker.bounded_items(items, one, should_stop=lambda: worker.cancelled(job['id']))
    if worker.cancelled(job['id']):
        worker.finish(job['id'], 'cancelled_partial')
        return
    with db.transaction(worker.settings.db_path) as conn:
        run = load(conn)
        validate(conn, worker.settings, run, job)
        stats = conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        complete = not worker.cancelled(job['id']) and stats['completed'] == len(items) and not stats['failed']
        for item in items:
            row = conn.execute('SELECT metadata,scan_id FROM tracks WHERE id=?', (item['track_id'],)).fetchone()
            meta = json.loads(row['metadata'])
            if row['scan_id'] == job['id']:
                meta['input_readiness'] = input_readiness.confirm(meta, job['id'], complete=complete,
                                                               grace_seconds=worker.settings.input_grace_seconds)
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), item['track_id']))
        conn.execute('INSERT INTO settings VALUES(?,?)', ('metadata_scan:' + job['id'], db.encode({'complete': complete, 'at': db.utcnow()})))
    worker.finish(job['id'], 'succeeded' if complete else 'cancelled_partial' if worker.cancelled(job['id']) else 'completed_with_errors')


def start_batch(conn, settings, active, key, tokens):
    from .worker import root_identity
    chosen = []
    kind = None
    connection = acoustid_connection(settings)
    for tid, token in tokens.items():
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (tid,)).fetchone()
        value = fields.evaluate(settings, conn, row, active) if row else None
        item = candidate(settings, conn, row, active, value, connection) if row else None
        if not item or item['token'] != token or kind and kind != item['kind']:
            raise security.error(409, 'preparation_stale', 'Обновите план: данные или доступные действия изменились')
        kind = item['kind']
        chosen.append({'track_id': tid, 'sha256': row['sha256'], 'relative_path': row['relative_path'],
                       'scan': item['scan'], 'guards': guards(conn, row),
                       **({'lyric_search_only': True} if item.get('lyric_search_only') is True else {}),
                       **({'expanded_lyrics': True} if item.get('expanded_lyrics') is True else {}),
                       **({'acoustid_continuation': item['acoustid_continuation']} if item.get('acoustid_continuation') else {}),
                       **({'embedded_recording_check': item['embedded_recording_check']} if item.get('embedded_recording_check') else {}),
                       **({'album_hint': item['album_hint']} if item.get('album_hint') else {}),
                       **({'recovery_of': item['recovery_of']} if item.get('recovery_of') else {})})
        conn.execute('INSERT INTO settings VALUES(?,?)', ('metadata_prepared:' + item['receipt'],
                     db.encode({'run_id': key, **({'recovery_of': item['recovery_of']} if item.get('recovery_of') else {})})))
    run = {'id': key, 'state': 'active', 'items': chosen, 'tokens': tokens, 'epoch': active['auth_epoch'],
           'root': root_identity(settings.work_root), 'kind': kind, 'total': len(chosen), 'completed_stages': [],
           'job_id': None, 'stage': STAGES[0], 'reason': None, 'started_at': db.utcnow()}
    try:
        validate(conn, settings, run)
    except media.MediaError as exc:
        raise security.error(409, exc.code, exc.message) from exc
    advance(conn, settings, run)
    save(conn, run)
    return public(run, conn)


def install(app, settings):
    @app.get('/api/metadata-preparation')
    def current(request: Request):
        security.require_session(request, settings, owner_only=True)
        from .worker import root_identity
        with db.connect(settings.db_path) as conn:
            result = public(load(conn), conn)
            stored = conn.execute("SELECT value FROM settings WHERE key='work_root_identity'").fetchone()
            try:
                result['root_reconciliation_required'] = bool(stored and json.loads(stored[0]) != root_identity(settings.work_root))
            except (media.MediaError, OSError):
                result['root_reconciliation_required'] = bool(stored)
            return result

    @app.post('/api/metadata-preparation', status_code=202)
    def start(request: Request, body: Start):
        active = security.require_session(request, settings, True, owner_only=True)
        key = request.headers.get('idempotency-key', '')
        try:
            if str(uuid.UUID(key)) != key:
                raise ValueError('noncanonical request ID')
        except ValueError:
            raise security.error(428, 'idempotency_key_required', 'Повторите запуск из интерфейса')
        with db.transaction(settings.db_path) as conn:
            previous = load(conn, key)
            if previous:
                if previous['tokens'] != body.tokens:
                    raise security.error(409, 'idempotency_conflict', 'Ключ принадлежит другому запуску')
                return public(previous, conn)
            if load(conn).get('state') == 'active':
                raise security.error(409, 'preparation_active', 'Подготовка уже запущена')
            return start_batch(conn, settings, active, key, body.tokens)

    @app.post('/api/metadata-preparation/cancel')
    def cancel(request: Request, body: Stop):
        security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            run = load(conn)
            if run.get('id') != body.id:
                raise security.error(409, 'preparation_stale', 'Открыта другая подготовка. Обновите её состояние.')
            if run.get('state') == 'active':
                run.update(state='stopped', reason='cancelled', finished_at=db.utcnow())
                if run.get('job_id'):
                    job = conn.execute('SELECT state,completed FROM jobs WHERE id=?', (run['job_id'],)).fetchone()
                    terminal = job and job['state'] in {'queued', 'paused', 'waiting_authorization'}
                    state = ('cancelled_partial' if job['completed'] else 'cancelled') if terminal else job['state'] if job else 'cancelled'
                    conn.execute('UPDATE jobs SET cancel_requested=1,state=?,updated_at=? WHERE id=?', (state, db.utcnow(), run['job_id']))
                save(conn, run)
            return public(run, conn)
