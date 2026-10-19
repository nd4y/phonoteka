"""One server gate for automatic application. No client can assert its evidence.

The production pilot registry is intentionally empty. Structural eligibility is
still evaluated in full, but an uncalibrated music pipeline never auto-applies.
Manual application retains its existing independent checks and workflow.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from . import db, field_policy, lyrics_document, lyrics_review, media, provider_policy, release_review, security, input_readiness, automation
from .providers import normalized, scalar

CONTRACT = 'automatic-quality-v1'
PROFILE = {'id': 'music-conservative-v1', 'version': 1, 'enabled': False, 'pilot_id': None,
           'languages': ['ru', 'en'], 'methods': ['whisper_attention_dtw_with_independent_asr_anchors_v1'],
           'minimum_line_probability': .65, 'minimum_token_probability': .15, 'maximum_words_per_second': 12,
           'maximum_line_seconds': 30, 'maximum_unexplained_gap_seconds': 45,
           'minimum_independent_token_recall': .9, 'minimum_independent_order_recall': .9,
           'maximum_tokens': 20000, 'threshold_status': 'not_music_pilot_validated'}
# Deployment-reviewed reports must enter a versioned release after the actual
# closed musical pilot. There is no HTTP/DB verified=true switch for this map.
PILOT_REGISTRY = {}
ALBUM_FIELDS = {'album', 'albumartist', 'date', 'tracknumber', 'discnumber', 'musicbrainz_albumid',
                'musicbrainz_releasetrackid', 'musicbrainz_releasegroupid'}


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def profile():
    value = deepcopy(PROFILE)
    value['profile_sha256'] = digest(PROFILE)
    value['pilot_registry_sha256'] = digest(PILOT_REGISTRY)
    return value


def pilot_validated(config):
    report = PILOT_REGISTRY.get(config.get('pilot_id'))
    return bool(report and report.get('profile_sha256') == config['profile_sha256'] and
                report.get('contract') == CONTRACT and report.get('acceptance_report_sha256') and
                report.get('closed_set_files') == 30 and report.get('languages') == {'ru': 15, 'en': 15} and
                report.get('negative_cases', 0) >= 10 and report.get('false_complete_acceptances') == 0 and
                report.get('acceptance_result') == 'passed')


def stored(conn, key, default=None):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def identity_review(conn, track_id):
    return stored(conn, 'automatic_identity:' + track_id, {'version': 0})


def latest_proposal(conn, track_id):
    return conn.execute('SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (track_id,)).fetchone()


def actor_snapshot(conn, active):
    epoch = int((conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone() or {'value': '0'})['value'])
    result = {'auth_epoch': active.get('auth_epoch'), 'current_auth_epoch': epoch,
              'token_id': active.get('api_token_id'), 'write_authorized': active.get('auth_epoch') == epoch}
    if active.get('auth_limited') or active.get('api_token_id') and 'auth_scopes' in active:
        result['write_authorized']=bool(result['write_authorized'] and automation.has_scope(active.get('auth_scopes'),'files:write'))
    if active.get('api_token_id'):
        token = conn.execute('SELECT auth_epoch,revoked,expires,scopes FROM api_tokens WHERE id=?', (active['api_token_id'],)).fetchone()
        if token:
            import time
            result.update(token=dict(token), write_authorized=bool(result['write_authorized'] and not token['revoked'] and token['expires'] > time.time() and token['auth_epoch'] == epoch and automation.has_scope(json.loads(token['scopes']), 'files:write')))
        else:
            result['write_authorized'] = False
    return result


def words(lines):
    return [line['text'] for line in lines if line['text'].strip()]


def occurrences(lines):
    if not isinstance(lines, list): return []
    return [(line.get('line_id'), line.get('text')) for line in lines if isinstance(line, dict) and isinstance(line.get('text'), str) and line['text'].strip()]


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def independent_comparison(reference, recognized, limit):
    expected = normalized(' '.join(reference)).split()
    actual = normalized(recognized).split()
    if not expected or not actual or len(expected) > limit or len(actual) > limit:
        return {'valid': False, 'token_recall': 0, 'order_recall': 0, 'reason': 'independent_asr_missing_or_oversized'}
    expected_counts, actual_counts = Counter(expected), Counter(actual)
    common = sum((expected_counts & actual_counts).values())
    positions = defaultdict(list)
    for index, word in enumerate(actual): positions[word].append(index)
    previous, matched = -1, 0
    for word in expected:
        available = positions[word]
        slot = bisect_right(available, previous)
        if slot < len(available):
            previous = available[slot]; matched += 1
    return {'valid': True, 'token_recall': common / len(expected), 'order_recall': matched / len(expected)}


def inference_bound(conn, track, asr):
    if not asr.get('remote_job_id') or asr.get('input_sha256') != track['sha256']:
        return False
    rows = conn.execute("SELECT j.kind,j.payload,ji.result FROM jobs j JOIN job_items ji ON ji.job_id=j.id WHERE ji.item_key=? AND ji.state='succeeded' AND j.kind IN ('transcribe','align') ORDER BY j.created_at DESC LIMIT 30", (track['id'],)).fetchall()
    for row in rows:
        result = json.loads(row['result'])
        payload = json.loads(row['payload'])
        if result.get('inference_job_id') == asr['remote_job_id'] and any(item.get('track_id') == track['id'] and item.get('sha256') == track['sha256'] for item in payload.get('items', [])):
            # These server modes call the unprompted transcriber before using
            # the reference in alignment. Client transcript flags are ignored.
            return True
    return False


def provider_dependencies(conn, payload, metadata, analysis, identity):
    references = (provider_policy.references(payload) + provider_policy.media_sources(metadata.get('usage_provenance', {})) +
                  analysis.get('alignment_usage', []))
    for candidate in analysis.get('candidates', []):
        if candidate.get('id') in {identity.get('recording_candidate_id'), identity.get('fingerprint_candidate_id')} and isinstance(candidate.get('usage'), dict):
            references.append(candidate['usage'])
    rules = {}
    pending = list(references); visited = 0
    while pending and visited < 1000:
        source = pending.pop(); visited += 1
        if not isinstance(source, dict): continue
        provider, kind = source.get('provider'), source.get('data_type', 'metadata')
        if provider: rules[provider + ':' + kind] = provider_policy.rule(conn, provider, kind)
        pending.extend(item for item in source.get('inherited_restrictions', []) if isinstance(item, dict))
    return {'references': references, 'rules': rules, 'overflow': bool(pending)}


def proposal_sources(payload, analysis, metadata):
    """Changed values must resolve to an intact server-stored candidate."""
    by_id = {candidate['id']: candidate for candidate in analysis.get('candidates', []) if candidate.get('id')}
    failures = []
    values = field_policy.payload_values(payload)
    for field, value in values.items():
        current = metadata.get('lrc') or metadata.get('lyrics') or '' if field == 'lrc' else metadata.get('tags', {}).get(field)
        if field != 'cover' and field_policy.normalized_value(field, value) == field_policy.normalized_value(field, current):
            continue
        source = payload.get('field_sources', {}).get(field, {})
        candidate = by_id.get(source.get('candidate_id'))
        candidate_value = candidate.get(field) if candidate and field in {'lrc', 'cover'} else candidate.get('tags', {}).get(field) if candidate else None
        if (not candidate or source.get('owner_edited') or source.get('response_sha256') != field_policy.value_hash(candidate) or
                source.get('value_sha256') != field_policy.value_hash(field_policy.normalized_value(field, value)) or
                field_policy.normalized_value(field, candidate_value) != field_policy.normalized_value(field, value)):
            failures.append(field)
    return failures


def recording_conditions(conn, row, payload, analysis, review, reasons):
    metadata = json.loads(row['metadata']); tags = {**metadata.get('tags', {}), **payload.get('tags', {})}
    candidates = {candidate['id']: candidate for candidate in analysis.get('candidates', []) if candidate.get('id')}
    recording = candidates.get(review.get('recording_candidate_id'), {})
    fingerprint = candidates.get(review.get('fingerprint_candidate_id'), {})
    for candidate in (recording, fingerprint):
        if candidate.get('usage'):
            try:
                provider_policy.require_sources(conn, [candidate['usage']], 'cache')
                provider_policy.require_sources(conn, [candidate['usage']], 'derived_analysis')
            except media.MediaError:
                reasons.append('recording_evidence_usage_denied')
    recording_id = recording.get('recording_id') or scalar(recording.get('tags', {}), 'musicbrainz_trackid')
    valid = bool(review.get('file_revision') == row['sha256'] and review.get('analysis_sha256') == digest(analysis) and
                 recording.get('provider') == 'musicbrainz' and re.fullmatch('[a-f0-9-]{36}', str(recording_id)) and
                 scalar(tags, 'musicbrainz_trackid') == recording_id and
                 fingerprint.get('identification') == 'audio_fingerprint' and fingerprint.get('match', {}).get('state') == 'fingerprint_match' and
                 normalized(scalar(recording.get('tags', {}), 'title')) == normalized(scalar(fingerprint.get('tags', {}), 'title')) and
                 normalized(scalar(recording.get('tags', {}), 'artist')) == normalized(scalar(fingerprint.get('tags', {}), 'artist')))
    if not valid: reasons.append('recording_identity_unverified')
    if (not number(recording.get('duration')) or not number(metadata.get('duration')) or
            abs(recording['duration'] - metadata['duration']) > 2 or
            'duration_differs' in recording.get('match', {}).get('reasons', [])):
        reasons.append('recording_duration_conflict_or_unknown')
    if review.get('performance_version') not in {'studio', 'live', 'edit', 'remaster', 'other_owner_reviewed'} or len(review.get('reason', '').strip()) < 5:
        reasons.append('performance_version_unverified')
    mode = review.get('edition_mode')
    release_snapshot = None
    if mode == 'recording_only':
        for field in ALBUM_FIELDS & payload.get('tags', {}).keys():
            if field_policy.normalized_value(field, payload['tags'][field]) != field_policy.normalized_value(field, metadata.get('tags', {}).get(field)):
                reasons.append('recording_only_album_write'); break
    elif mode == 'release':
        value = stored(conn, 'release_review:' + str(review.get('release_review_id')))
        if value:
            report = release_review.report(conn, value)
            release_snapshot = {'id': value['id'], 'version': value['version'], 'sha256': digest(value), 'state': report['state']}
            if (value['version'] != review.get('release_review_version') or report['state'] not in {'complete', 'subset_complete'} or
                    row['id'] not in report.get('matched_track_ids', []) or scalar(tags, 'musicbrainz_albumid') != value['release_id']):
                reasons.append('release_selection_unverified')
            if not any(entry.get('recording_id') == recording_id and any(file['track_id'] == row['id'] for file in entry.get('files', [])) for entry in report.get('tracks', [])):
                reasons.append('release_recording_conflict')
        else:
            reasons.append('release_selection_unverified')
    else:
        reasons.append('explicit_edition_mode_required')
    return release_snapshot


def evaluate(settings, conn, row, proposal, active):
    config = profile(); reasons = []; blocked = []
    metadata = json.loads(row['metadata']); payload = json.loads(proposal['payload']) if proposal else {}
    analysis_row = conn.execute('SELECT * FROM analyses WHERE track_id=?', (row['id'],)).fetchone()
    analysis = json.loads(analysis_row['payload']) if analysis_row and analysis_row['input_sha256'] == row['sha256'] else {}
    canonical = lyrics_document.ensure(conn, row)
    review = lyrics_review.current_review(row, lyrics_review.load(conn, row['id']))
    identity = identity_review(conn, row['id'])
    actor = actor_snapshot(conn, active)
    if not config['enabled']: blocked.append('automatic_apply_disabled')
    if not pilot_validated(config): blocked.append('pilot_not_validated')
    if not actor['write_authorized']: blocked.append('authorization_write_required')
    if not settings.writable: blocked.append('storage_write_disabled')
    try: media._expect(media._validate(settings.work_root / row['relative_path'], settings.roots), row['sha256'])
    except (media.MediaError, OSError): blocked.append('file_revision_stale')
    if row['status'] in {'conflict', 'error', 'missing'} or metadata.get('input_error') or metadata.get('critical_conflicts'):
        blocked.append('critical_file_conflict')
    if not metadata.get('write_supported') or metadata.get('format') == 'cue' or metadata.get('cue_album_image'):
        blocked.append('format_not_writable')
    audit = metadata.get('audit') or {}
    if audit.get('state') != 'passed' or audit.get('sha256') != row['sha256']: reasons.append('current_audio_audit_required')
    latest = latest_proposal(conn, row['id'])
    if not proposal or proposal['input_sha256'] != row['sha256'] or not latest or latest['id'] != proposal['id']:
        blocked.append('proposal_stale_or_missing')
    if not analysis: reasons.append('current_analysis_required')
    if input_readiness.describe(metadata,grace_seconds=settings.input_grace_seconds)['state']!='ready':
        reasons.append('input_stabilizing')
    if analysis.get('critical_conflicts') or analysis.get('unresolved_conflicts'):
        reasons.append('analysis_conflict_unresolved')
    if analysis.get('possible_mix') or metadata.get('possible_mix') or analysis.get('multiple_recordings'):
        reasons.append('possible_mix_requires_manual_review')
    locks = field_policy.load(conn, row['id']); priorities = field_policy.sources(conn)
    if payload.get('source_priority_version', priorities['version']) != priorities['version']:
        blocked.append('source_priority_stale')
    try: field_policy.assert_apply(settings, row, payload, conn=conn)
    except media.MediaError as exc: blocked.append('manual_lock_conflict:' + exc.code)
    usage = provider_dependencies(conn, payload, metadata, analysis, identity)
    if usage['overflow']: blocked.append('source_policy_depth_exceeded')
    try:
        provider_policy.require_payload(conn, row, payload, 'embed')
        provider_policy.require_sources(conn, analysis.get('alignment_usage', []), 'derived_analysis')
    except media.MediaError: blocked.append('provider_usage_denied')
    changed_untrusted = proposal_sources(payload, analysis, metadata)
    if changed_untrusted: blocked.append('proposal_not_server_candidate')
    tag_changes = any(field_policy.normalized_value(field, value) != field_policy.normalized_value(field, metadata.get('tags', {}).get(field)) for field, value in payload.get('tags', {}).items())
    release_snapshot = recording_conditions(conn, row, payload, analysis, identity, reasons) if tag_changes or payload.get('cover') else None
    if payload.get('cover'):
        source = payload.get('field_sources', {}).get('cover', {})
        candidate = next((candidate for candidate in analysis.get('candidates', []) if candidate.get('id') == source.get('candidate_id')), {})
        if identity.get('edition_mode') != 'release' or candidate.get('release_id') != scalar({**metadata.get('tags', {}), **payload.get('tags', {})}, 'musicbrainz_albumid'):
            reasons.append('cover_release_unverified')
    evidence = {}
    if canonical['lines'] and analysis.get('asr', {}).get('text'):
        independent = independent_comparison(words(canonical['lines']), analysis['asr']['text'], config['maximum_tokens'])
        if not independent['valid'] or independent['token_recall'] < config['minimum_independent_token_recall'] or independent['order_recall'] < config['minimum_independent_order_recall']:
            reasons.append('independent_asr_contradiction')
    if payload.get('lrc') is not None:
        try:
            draft = lyrics_document.payload_document(conn, row, payload)
            lines = draft['lines']; accepted = review['lines']
            if words(lines) != words(canonical['lines']) or words(canonical['lines']) != words(accepted) or not all(line.get('words_state') == 'accepted' for line in accepted if line['text'].strip()):
                reasons.append('previously_accepted_words_required')
            alignment = analysis.get('alignment') or {}; asr = analysis.get('asr') or {}
            if (alignment.get('stale') or alignment.get('words_revision') != canonical['words_revision'] or
                    alignment.get('document_id') != canonical['document_id'] or occurrences(alignment.get('lines', [])) != occurrences(lines)):
                reasons.append('alignment_dependency_stale')
            language = asr.get('language')
            if language not in config['languages'] or identity.get('language') != language:
                reasons.append('language_unsupported_or_unconfirmed')
            if alignment.get('method') not in config['methods']: reasons.append('aligner_profile_unsupported')
            if not inference_bound(conn, row, asr): reasons.append('independent_asr_provenance_missing')
            comparison = independent_comparison(words(lines), asr.get('text', ''), config['maximum_tokens'])
            evidence['independent_comparison'] = comparison
            if not comparison['valid'] or comparison['token_recall'] < config['minimum_independent_token_recall'] or comparison['order_recall'] < config['minimum_independent_order_recall']:
                reasons.append('independent_asr_contradiction')
            previous = -1; previous_end = None; reliable = 0; meaningful = 0
            expected = {line['line_id']: line for line in lines}
            aligned_lines = alignment.get('lines')
            if not isinstance(aligned_lines, list) or len(aligned_lines) > media.MAX_LRC_LINES or any(not isinstance(line, dict) for line in aligned_lines):
                aligned_lines = []
                reasons.append('alignment_result_malformed')
            for line in aligned_lines:
                if not isinstance(line.get('text'), str) or not isinstance(line.get('line_id'), str):
                    reasons.append('alignment_result_malformed'); continue
                if not line.get('text', '').strip(): continue
                meaningful += 1; start, end = line.get('start_ms'), line.get('end_ms')
                valid_time = (isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int) and not isinstance(end, bool) and
                              number(metadata.get('duration')) and 0 <= start < end <= metadata['duration'] * 1000 and start >= previous)
                if not valid_time or expected.get(line.get('line_id'), {}).get('start_ms') != start:
                    reasons.append('timing_invalid_or_not_proposed'); continue
                if previous_end is not None and start - previous_end > config['maximum_unexplained_gap_seconds'] * 1000:
                    reasons.append('unexplained_timing_gap')
                previous, previous_end = start, end
                seconds = (end - start) / 1000
                if (line.get('state') != 'aligned' or type(line.get('low_probability_words')) is not int or line['low_probability_words'] != 0 or
                        not number(line.get('probability')) or not config['minimum_line_probability'] <= line['probability'] <= 1 or
                        not number(line.get('minimum_token_probability')) or not config['minimum_token_probability'] <= line['minimum_token_probability'] <= 1 or
                        seconds > config['maximum_line_seconds'] or len(normalized(line['text']).split()) / seconds > config['maximum_words_per_second']):
                    reasons.append('line_quality_below_profile'); continue
                reliable += 1
            content_count = len(words(lines))
            if not content_count or reliable != content_count or meaningful != content_count or not draft['complete'] or not draft['timing_order_valid']:
                reasons.append('complete_reliable_timings_required')
            evidence.update(content_lines=content_count, reliable_lines=reliable, language=language)
        except media.MediaError as exc:
            blocked.append('lyrics_document_invalid:' + exc.code)
    dependencies = {'contract': CONTRACT, 'profile': config, 'file_revision': row['sha256'], 'metadata_sha256': digest(metadata),
                    'proposal_id': proposal['id'] if proposal else None, 'proposal_sha256': digest(payload),
                    'latest_proposal_id': latest['id'] if latest else None, 'analysis_sha256': digest(analysis),
                    'field_policy': locks, 'source_priorities': priorities, 'lyrics_document': {'version': canonical['version'],
                        'words_revision': canonical['words_revision'], 'timings_revision': canonical['timings_revision'], 'locks': canonical.get('locks', {})},
                    'lyrics_review': review, 'identity_review': identity, 'release': release_snapshot, 'usage': usage, 'authorization': actor}
    codes = list(dict.fromkeys(blocked + reasons))
    return {'contract': CONTRACT, 'state': 'blocked' if blocked else 'needs_review' if reasons else 'passed',
            'reason_codes': codes, 'profile': config, 'dependency_sha256': digest(dependencies),
            'proposal_id': proposal['id'] if proposal else None, 'file_revision': row['sha256'], 'evidence': evidence,
            'untrusted_fields': changed_untrusted, 'automatic_acceptance': not codes}


def freeze(settings, conn, row, proposal, active, *, applied_payload=None):
    result = evaluate(settings, conn, row, proposal, active)
    if result['state'] != 'passed':
        raise media.MediaError('automatic_quality_blocked', 'Automatic quality gate: ' + ', '.join(result['reason_codes']))
    return {**{key: result[key] for key in ('contract', 'profile', 'dependency_sha256', 'proposal_id', 'file_revision')},
            'apply_payload_sha256': digest(applied_payload if applied_payload is not None else json.loads(proposal['payload']))}


def assert_frozen(settings, row, item, job):
    if item.get('acceptance_mode', 'manual') != 'automatic': return
    frozen = item.get('automatic_quality')
    if not frozen:
        raise media.MediaError('automatic_quality_freeze_missing', 'Отсутствует серверный snapshot автоматического принятия')
    active = {'auth_epoch': job['auth_epoch']}
    payload=json.loads(job['payload'])
    token = payload.get('auth_token_id')
    if token: active['api_token_id'] = token
    if 'auth_scopes' in payload:active['auth_scopes']=payload['auth_scopes']
    if payload.get('auth_limited'):active['auth_limited']=True
    with db.connect(settings.db_path) as conn:
        fresh = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (row['id'],)).fetchone()
        proposal = conn.execute('SELECT * FROM proposals WHERE id=? AND track_id=?', (item.get('proposal_id'), row['id'])).fetchone()
        if not fresh or not proposal:
            raise media.MediaError('automatic_quality_dependency_stale', 'Файл/предложение автоматического принятия недоступны')
        result = evaluate(settings, conn, fresh, proposal, active)
    if (result['state'] != 'passed' or result['dependency_sha256'] != frozen['dependency_sha256'] or result['profile'] != frozen['profile'] or
            digest(item.get('proposal', {})) != frozen.get('apply_payload_sha256')):
        raise media.MediaError('automatic_quality_dependency_stale', 'Автоматическое принятие устарело: ' + ', '.join(result['reason_codes']))
    return result


class IdentityInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    recording_candidate_id: str = Field(min_length=1, max_length=150)
    fingerprint_candidate_id: str = Field(min_length=1, max_length=150)
    performance_version: str = Field(pattern='^(studio|live|edit|remaster|other_owner_reviewed)$')
    edition_mode: str = Field(pattern='^(recording_only|release)$')
    release_review_id: str | None = Field(default=None, max_length=36)
    release_review_version: int | None = Field(default=None, ge=1)
    language: str = Field(pattern='^(ru|en)$')
    reason: str = Field(min_length=5, max_length=1000)


def install(app, settings):
    @app.get('/api/automatic-quality/profile')
    def current_profile(request: Request):
        security.require_session(request, settings)
        value = profile()
        return {**value, 'pilot_validated': pilot_validated(value), 'owner_editable': False}

    @app.get('/api/tracks/{track_id}/automatic-quality')
    def preview(request: Request, track_id: str, proposal_id: str | None = None):
        active = security.require_session(request, settings)
        with db.connect(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row: raise security.error(404, 'track_not_found', 'Трек не найден')
            proposal = conn.execute('SELECT * FROM proposals WHERE id=? AND track_id=?', (proposal_id, track_id)).fetchone() if proposal_id else latest_proposal(conn, track_id)
            result = evaluate(settings, conn, row, proposal, active)
            result['identity_review'] = identity_review(conn, track_id)
            analysis = conn.execute('SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?', (track_id, row['sha256'])).fetchone()
            candidates = provider_policy.prepare_analysis(conn, json.loads(analysis['payload']), register=False).get('candidates', []) if analysis else []
            result['identity_options'] = [{'id': candidate['id'], 'provider': candidate['provider'], 'title': scalar(candidate.get('tags', {}), 'title'),
                'artist': scalar(candidate.get('tags', {}), 'artist'), 'kind': 'recording' if candidate.get('provider') == 'musicbrainz' else 'fingerprint'}
                for candidate in candidates if candidate.get('provider') == 'musicbrainz' or candidate.get('identification') == 'audio_fingerprint']
        return result

    @app.put('/api/tracks/{track_id}/automatic-quality/identity')
    def save_identity(request: Request, track_id: str, body: IdentityInput):
        security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row: raise security.error(404, 'track_not_found', 'Трек не найден')
            if not request.headers.get('if-match'): raise security.error(428, 'revision_required', 'Передайте FileRevision')
            if request.headers['if-match'].strip('"') != row['sha256']: raise security.error(412, 'revision_conflict', 'Файл изменился')
            previous = identity_review(conn, track_id)
            if previous['version'] != body.base_version: raise security.error(412, 'decision_conflict', 'Проверка записи изменилась')
            analysis = conn.execute('SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?', (track_id, row['sha256'])).fetchone()
            if not analysis: raise security.error(409, 'current_analysis_required', 'Нужны актуальные серверные результаты поиска')
            data = json.loads(analysis['payload']); candidates = {item['id']: item for item in data.get('candidates', [])}
            recording, fingerprint = candidates.get(body.recording_candidate_id, {}), candidates.get(body.fingerprint_candidate_id, {})
            if recording.get('provider') != 'musicbrainz' or fingerprint.get('identification') != 'audio_fingerprint' or fingerprint.get('match', {}).get('state') != 'fingerprint_match':
                raise security.error(422, 'recording_evidence_required', 'Выберите серверный MusicBrainz recording и совпадение по аудиоотпечатку')
            value = {**body.model_dump(), 'version': previous['version'] + 1, 'file_revision': row['sha256'],
                     'analysis_sha256': digest(data), 'origin': 'owner_recording_review', 'reviewed_at': db.utcnow(),
                     'automatic_acceptance': False, 'license_verified': False}
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('automatic_identity:' + track_id, db.encode(value)))
            db.event(conn, 'recording_reviewed', {'track_id': track_id, 'version': value['version'], 'edition_mode': value['edition_mode']})
        return value
