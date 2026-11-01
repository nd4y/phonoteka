"""Owner listening decisions bound to exact audio, words and timing revisions.

Presence of LRC timestamps is never a human or Poweramp verification. This module
does not modify music, proposals or model results; publication must call the gate
again immediately before copying the accepted file revision.
"""
from collections import defaultdict
import hashlib
import json
import uuid
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from . import db, media, provider_policy, security, recording_review

POLICY_VERSION = 'owner-listening-review-v1'
PROFILES = ('lyrics_required', 'instrumental', 'relaxed')
STATES = ('unknown', 'accepted', 'uncertain')
_NO_FROZEN_REVIEW = object()


def digest(value):
    return hashlib.sha256(db.encode(value).encode('utf-8')).hexdigest()


def document(row):
    meta = json.loads(row['metadata'])
    raw = meta.get('lrc') or meta.get('lyrics') or ''
    error = None
    try:
        parsed = media.parse_lrc(raw)
        source = parsed['lines']
    except (media.MediaError, ValueError) as exc:
        source = []
        error = getattr(exc, 'code', 'invalid_lyrics')
    seen = defaultdict(int)
    lines = []
    previous = -1
    duration = meta.get('duration')
    for ordinal, item in enumerate(source):
        text = item['text']
        seen[text] += 1
        stamp = item.get('start_ms')
        valid = isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0
        if valid and duration and stamp > duration * 1000:
            valid = False
        if stamp is not None and (not valid or stamp < previous):
            error = 'invalid_timing'
        if valid:
            previous = stamp
        lines.append({'line_id': digest([row['id'], text, seen[text]])[:24], 'ordinal': ordinal,
                      'text': text, 'start_ms': stamp, 'timing_valid': valid, 'content': bool(text.strip())})
    words_revision = digest([line['text'] for line in lines])
    timings_revision = digest([[line['line_id'], line['start_ms']] for line in lines])
    return {'file_revision': row['sha256'], 'words_revision': words_revision, 'timings_revision': timings_revision,
            'document_revision': digest([row['sha256'], words_revision, timings_revision]),
            'lines': lines, 'parse_error': error, 'has_text': bool(raw.strip()),
            'complete': bool(lines) and all(line['timing_valid'] for line in lines),
            'audio_identity': (meta.get('audit') if isinstance(meta.get('audit'), dict) else {}).get('audio_identity')}


def load(conn, track_id):
    row = conn.execute('SELECT value FROM settings WHERE key=?', ('lyrics_review:' + track_id,)).fetchone()
    return json.loads(row['value']) if row else {'version': 0, 'profile': 'lyrics_required', 'reason': '',
                                               'instrumental_confirmed': False, 'decisions': {},
                                               'listened_sections': [], 'accepted_revision': None}


def current_review(row, saved):
    current = document(row)
    stale = bool(saved.get('document_revision')) and saved['document_revision'] != current['document_revision']
    decisions = saved.get('decisions', {}) if not stale else {}
    lines = []
    for line in current['lines']:
        decision = decisions.get(line['line_id'], {})
        lines.append({**line, 'words_state': decision.get('words_state', 'unknown'),
                      'timing_state': decision.get('timing_state', 'unknown')})
    return {**saved, **current, 'lines': lines, 'stale': stale,
            'instrumental_confirmed': saved.get('instrumental_confirmed', False) if not stale else False,
            'accepted_current_revision': not stale and saved.get('accepted_revision') == row['sha256'],
            'listened_sections': saved.get('listened_sections', []) if not stale else [],
            'human_timing_verified': not stale and bool([line for line in lines if line['content']])
                and all(line['words_state'] == line['timing_state'] == 'accepted' and line['timing_valid'] for line in lines if line['content']),
            'poweramp_verified': False, 'poweramp_verification_source': 'separate_delivery_receipt',
            'automatic_acceptance': False, 'policy_version': POLICY_VERSION}


def publication_gate(settings, row, *, conn=None, audit=None):
    if conn is None:
        with db.connect(settings.db_path) as active:
            return publication_gate(settings, row, conn=active, audit=audit)
    meta = json.loads(row['metadata'])
    review = current_review(row, load(conn, row['id']))
    reasons = []
    blocked = []
    if recording_review.possible_mix(meta,row['sha256']):
        blocked.append('possible_mix_requires_manual_review')
    if row['status'] in {'conflict', 'error', 'missing'} or meta.get('input_error') or meta.get('critical_conflicts'):
        blocked.append('critical_file_conflict')
    try:
        provider_policy.require_track(settings, row, 'export', conn=conn)
    except media.MediaError:
        blocked.append('provider_export_denied')
    checked = audit if audit is not None else meta.get('audit')
    checked = checked if isinstance(checked, dict) else {}
    if checked.get('state') in {'failed', 'unsupported'} and checked.get('sha256') == row['sha256']:
        blocked.append('decode_audit_failed')
    if checked.get('state') != 'passed' or checked.get('sha256') != row['sha256']:
        reasons.append('current_decode_audit_required')
    if not review['accepted_current_revision']:
        reasons.append('accepted_file_revision_required')
    if review['parse_error']:
        blocked.append(review['parse_error'])
    def tag_present(key):
        value = meta.get('tags', {}).get(key)
        values = value if isinstance(value, list) else [value]
        return any(value is not None and str(value).strip() for value in values)
    missing = [key for key in ('title', 'artist', 'album') if not tag_present(key)]
    if not meta.get('has_art'):
        missing.append('cover')
    profile = review['profile']
    if profile != 'relaxed':
        reasons.extend('missing_' + field for field in missing)
    if profile == 'lyrics_required':
        if not review['has_text']:
            reasons.append('lyrics_required')
        if not review['complete']:
            reasons.append('complete_synced_lyrics_required')
        if not review['human_timing_verified']:
            reasons.append('line_listening_review_required')
        if set(review['listened_sections']) != {'start', 'middle', 'end'}:
            reasons.append('start_middle_end_listening_required')
    elif profile == 'instrumental':
        if not review['instrumental_confirmed'] or not review.get('reason', '').strip():
            reasons.append('owner_instrumental_confirmation_required')
        if any(line['content'] for line in review['lines']):
            reasons.append('instrumental_has_lyrics_conflict')
    elif profile == 'relaxed':
        if not review.get('reason', '').strip():
            reasons.append('owner_exception_reason_required')
    else:
        blocked.append('unknown_publication_profile')
    exemptions = []
    if profile == 'instrumental':
        exemptions = ['lyrics_not_required_owner_confirmed_instrumental']
    elif profile == 'relaxed':
        exemptions = ['owner_exception:' + reason for reason in missing]
        if not review['human_timing_verified']:
            exemptions.append('owner_exception:lyrics_not_human_timing_verified')
    return {'state': 'blocked' if blocked else 'needs_review' if reasons else 'passed',
            'reasons': list(dict.fromkeys(blocked + reasons)), 'profile': profile, 'policy_version': POLICY_VERSION,
            'file_revision': row['sha256'], 'review_version': review['version'], 'document_revision': review['document_revision'],
            'human_timing_verified': review['human_timing_verified'], 'instrumental_confirmed': review['instrumental_confirmed'],
            'exceptions': exemptions, 'exception_reason': review.get('reason', ''), 'automatic_acceptance': False}


def freeze_publication(settings, row, *, conn=None):
    """Freeze owner decisions, without copying lyric words into a queued job.

    A successful audit is deliberately evaluated at execution, independently of
    this snapshot. A newer review must never silently approve an older queue item.
    """
    if conn is None:
        with db.connect(settings.db_path) as active:
            return freeze_publication(settings, row, conn=active)
    saved = load(conn, row['id'])
    current = document(row)
    snapshot = {'contract': 'publication-review-v1', 'track_id': row['id'],
                'file_revision': row['sha256'], 'document_revision': current['document_revision'],
                'policy_version': POLICY_VERSION, 'review_version': saved['version'],
                'profile': saved['profile'], 'review_sha256': digest(saved)}
    return {**snapshot, 'snapshot_sha256': digest(snapshot)}


def assert_publication_frozen(settings, row, frozen, *, conn=None):
    if not isinstance(frozen, dict):
        raise media.MediaError('publication_snapshot_required', 'У задания нет зафиксированной приемки публикации. Создайте новое задание')
    expected_hash = digest({key: value for key, value in frozen.items() if key != 'snapshot_sha256'})
    if frozen.get('snapshot_sha256') != expected_hash or freeze_publication(settings, row, conn=conn) != frozen:
        raise media.MediaError('publication_review_stale', 'Приемка, профиль или политика публикации изменились после постановки в очередь. Проверьте файл и создайте новое задание')
    return frozen


def assert_publish_allowed(settings, row, *, audit=None, frozen=_NO_FROZEN_REVIEW):
    snapshot = None if frozen is _NO_FROZEN_REVIEW else assert_publication_frozen(settings, row, frozen)
    gate = publication_gate(settings, row, audit=audit)
    if gate['state'] != 'passed':
        raise media.MediaError('needs_review', 'Профиль публикации: ' + ', '.join(gate['reasons']))
    if snapshot is not None:
        gate['review_snapshot_sha256'] = snapshot['snapshot_sha256']
    return gate


class LineDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    line_id: str = Field(min_length=24, max_length=24)
    words_state: Literal['unknown', 'accepted', 'uncertain']
    timing_state: Literal['unknown', 'accepted', 'uncertain']


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    document_revision: str = Field(min_length=64, max_length=64)
    lines: list[LineDecision] = Field(default_factory=list, max_length=20000)
    listened_sections: list[Literal['start', 'middle', 'end']] = Field(default_factory=list, max_length=3)
    profile: Literal['lyrics_required', 'instrumental', 'relaxed'] = 'lyrics_required'
    reason: str = Field(default='', max_length=1000)
    instrumental_confirmed: bool = False
    accept_revision: bool = False


def install(app, settings):
    @app.get('/api/tracks/{track_id}/lyrics-review')
    def get_review(request: Request, track_id: str):
        security.require_session(request, settings, owner_only=True)
        with db.connect(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row:
                raise security.error(404, 'track_not_found', 'Трек не найден')
            row = dict(row)
            return {**current_review(row, load(conn, track_id)), 'track_id': track_id,
                    'gate': publication_gate(settings, row, conn=conn), 'profiles': list(PROFILES)}

    @app.put('/api/tracks/{track_id}/lyrics-review')
    def put_review(request: Request, track_id: str, body: ReviewInput):
        security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row:
                raise security.error(404, 'track_not_found', 'Трек не найден')
            row = dict(row)
            if request.headers.get('if-match', '').strip('"') != row['sha256']:
                raise security.error(412, 'revision_conflict', 'Файл изменился. Обновите проверку текста')
            saved = load(conn, track_id)
            current = current_review(row, saved)
            if body.base_version != saved['version'] or body.document_revision != current['document_revision']:
                raise security.error(412, 'lyrics_review_conflict', 'Текст, времена или проверка изменились. Обновите карточку')
            busy = conn.execute("SELECT 1 FROM jobs j,json_each(j.payload,'$.items') i WHERE j.kind IN ('apply','rollback','publish') AND j.state IN ('queued','running','paused','waiting_authorization','interrupted') AND json_extract(i.value,'$.track_id')=? LIMIT 1", (track_id,)).fetchone()
            if busy:
                raise security.error(409, 'track_mutation_pending', 'Дождитесь завершения операции с файлом')
            indexed = {line['line_id']: line for line in current['lines']}
            ids = [line.line_id for line in body.lines]
            if len(ids) != len(set(ids)) or set(ids) - set(indexed) or len(body.listened_sections) != len(set(body.listened_sections)):
                raise security.error(422, 'invalid_line_decision', 'Неизвестные или повторные строки/участки проверки')
            decisions = dict(saved.get('decisions', {})) if not current['stale'] else {}
            for decision in body.lines:
                if decision.timing_state == 'accepted' and not indexed[decision.line_id]['timing_valid']:
                    raise security.error(422, 'line_timing_required', 'Нельзя принять строку без допустимого времени')
                decisions[decision.line_id] = {'words_state': decision.words_state, 'timing_state': decision.timing_state}
            if body.profile in {'instrumental', 'relaxed'} and not body.reason.strip():
                raise security.error(422, 'exception_reason_required', 'Для выбранного профиля укажите причину')
            if body.profile == 'instrumental' and not body.instrumental_confirmed:
                raise security.error(422, 'instrumental_confirmation_required', 'Подтвердите инструментальную запись после прослушивания')
            result = {'version': saved['version'] + 1, 'file_revision': row['sha256'],
                      'document_revision': current['document_revision'], 'words_revision': current['words_revision'],
                      'timings_revision': current['timings_revision'], 'audio_identity': current['audio_identity'],
                      'decisions': decisions, 'listened_sections': body.listened_sections, 'profile': body.profile,
                      'reason': body.reason.strip(), 'instrumental_confirmed': body.instrumental_confirmed,
                      'accepted_revision': row['sha256'] if body.accept_revision else saved.get('accepted_revision') if not current['stale'] else None,
                      'updated_at': db.utcnow(), 'policy_version': POLICY_VERSION}
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('lyrics_review:' + track_id, db.encode(result)))
            gate = publication_gate(settings, row, conn=conn)
            if body.accept_revision and any(reason in gate['reasons'] for reason in ['line_listening_review_required', 'start_middle_end_listening_required', 'complete_synced_lyrics_required', 'lyrics_required', 'instrumental_has_lyrics_conflict']):
                raise security.error(409, 'lyrics_review_incomplete', 'Сначала проверьте строки и начало, середину и конец записи')
            conn.execute('INSERT INTO history(id,track_id,kind,before_sha256,after_sha256,details,created_at) VALUES(?,?,?,?,?,?,?)',
                         (str(uuid.uuid4()), track_id, 'lyrics_review', row['sha256'], row['sha256'],
                          db.encode({'review': result, 'publication_gate': gate, 'music_changed': False}), result['updated_at']))
            db.event(conn, 'lyrics_review_changed', {'track_id': track_id, 'version': result['version'], 'profile': result['profile'], 'state': gate['state']})
            return {**current_review(row, result), 'track_id': track_id, 'gate': gate, 'profiles': list(PROFILES)}
