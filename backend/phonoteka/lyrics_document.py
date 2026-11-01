"""Durable lyric occurrences and independent owner locks, separate from LRC bytes.

Only embedded words live in the canonical document. Provider-derived proposed
words stay in proposals and expire with their source payload, never in settings
or the history of lock changes.
"""
from copy import deepcopy
from collections import defaultdict, deque
import hashlib
import json
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import db, media, security


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def key(track_id):
    return 'lyrics_document:' + track_id


def raw_text(row):
    meta = json.loads(row['metadata'])
    return meta.get('lrc') or meta.get('lyrics') or ''


def parse(raw):
    parsed = media.parse_lrc(raw)
    return [{'line_id': line['line_id'], 'ordinal': index, 'text': line['text'], 'start_ms': line['start_ms'],
             'words_state': 'raw', 'timing_state': 'raw' if line['start_ms'] is not None else 'unaligned'}
            for index, line in enumerate(parsed['lines'])], parsed['metadata']


def remap(previous, incoming, track_id, seed):
    """Legacy import reconciles exact occurrences, then unchanged ordinal slots.

    Structured editing supplies IDs directly and never runs this reconciliation.
    Duplicate text is consumed in existing occurrence order, never by timestamp.
    """
    remaining = {line['line_id']: line for line in previous}
    by_ordinal = {line['ordinal']: line for line in previous}
    by_text = defaultdict(deque)
    for line in previous:
        by_text[line['text']].append(line)
    result = []
    for index, item in enumerate(incoming):
        exact = by_text[item['text']].popleft() if by_text[item['text']] else None
        if exact:
            remaining.pop(exact['line_id'])
        result.append((item, exact))
    for index, (item, previous_line) in enumerate(result):
        if previous_line is None:
            candidate = by_ordinal.get(index)
            previous_line = candidate if candidate and candidate['line_id'] in remaining else None
            if previous_line:
                remaining.pop(previous_line['line_id'])
        identity = previous_line['line_id'] if previous_line else str(uuid.uuid5(uuid.NAMESPACE_URL, f'phonoteka:{track_id}:{seed}:{index}'))
        result[index] = {**item, 'line_id': identity, 'ordinal': index}
    return result


def content(lines):
    return [{'line_id': line['line_id'], 'ordinal': index, 'text': line['text'], 'start_ms': line['start_ms'],
             'words_state': line.get('words_state', 'proposed'),
             'timing_state': line.get('timing_state', 'proposed' if line['start_ms'] is not None else 'unaligned')}
            for index, line in enumerate(lines)]


def revisions(lines):
    return {'words_revision': digest([[line['line_id'], line['text']] for line in lines]),
            'timings_revision': digest([[line['line_id'], line['start_ms']] for line in lines]),
            'document_revision': digest([[line['line_id'], line['text'], line['start_ms']] for line in lines])}


def ensure(conn, row):
    stored = conn.execute('SELECT value FROM settings WHERE key=?', (key(row['id']),)).fetchone()
    current = json.loads(stored['value']) if stored else None
    raw = raw_text(row)
    source_hash = digest(raw)
    if current and current['embedded_sha256'] == source_hash and current['file_revision'] == row['sha256']:
        return current
    if current and current['embedded_sha256'] == source_hash:
        current.update(version=current['version'] + 1, file_revision=row['sha256'])
        conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(current), key(row['id'])))
        return current
    try:
        lines, metadata = parse(raw)
        lines = remap(current['lines'] if current else [], lines, row['id'], source_hash)
        error = None
    except media.MediaError as exc:
        lines, metadata, error = [], {}, exc.code
    meta = json.loads(row['metadata'])
    value = {'contract': 'lyrics-document-v1', 'document_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'phonoteka:lyrics:' + row['id'])),
             'version': current['version'] + 1 if current else 0, 'file_revision': row['sha256'],
             'embedded_sha256': source_hash, 'lines': lines, 'metadata': metadata, 'parse_error': error,
             'locks': current.get('locks', {}) if current else {}, 'source': 'embedded', **revisions(lines)}
    value.update(language=meta.get('lyrics_language'), provenance=meta.get('usage_provenance', {}).get('lrc', {'origin': 'preexisting'}),
                 audio_identity=(meta.get('audit') or {}).get('audio_identity'))
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key(row['id']), db.encode(value)))
    return value


def render(lines):
    # Drafts preserve ordinal and nullable lines. Audio embedding still runs the
    # strict media serializer, which refuses partial/nonmonotonic synchronized LRC.
    return '\n'.join((f"[{line['start_ms']//60000:02}:{line['start_ms']//1000%60:02}.{line['start_ms']%1000:03}]" if line['start_ms'] is not None else '') + line['text'] for line in lines)


def view(value, state, row):
    result = deepcopy(value)
    result.update(version=state['version'], file_revision=row['sha256'], locks=state.get('locks', {}))
    result.update(revisions(result['lines']))
    result['complete'] = bool(result['lines']) and all(line['start_ms'] is not None for line in result['lines'] if line['text'].strip())
    result['lrc'] = render(result['lines'])
    result['timing_order_valid'] = all(a['start_ms'] <= b['start_ms'] for a, b in zip(
        [line for line in result['lines'] if line['start_ms'] is not None],
        [line for line in result['lines'] if line['start_ms'] is not None][1:]))
    return result


def effective(conn, row, state=None):
    state = state or ensure(conn, row)
    latest = conn.execute('SELECT id,payload FROM proposals WHERE track_id=? AND input_sha256=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (row['id'], row['sha256'])).fetchone()
    if latest:
        payload = json.loads(latest['payload'])
        if 'lrc' in payload and payload['lrc'] is not None:
            if payload.get('lyrics_document'):
                draft = {**state, **payload['lyrics_document'], 'source': 'proposal', 'proposal_id': latest['id']}
            else:
                try:
                    lines, metadata = parse(payload['lrc'])
                    draft = {**state, 'lines': remap(state['lines'], lines, row['id'], digest(payload['lrc'])), 'metadata': metadata,
                             'source': 'proposal', 'proposal_id': latest['id'], 'parse_error': None}
                except media.MediaError:
                    return view(state, state, row)
            return view(draft, state, row)
    return view(state, state, row)


def public_document(settings, row):
    with db.connect(settings.db_path) as conn:
        return effective(conn, row)


def validate_lines(lines, row):
    if len(lines) > media.MAX_LRC_LINES:
        raise media.MediaError('invalid_lyrics', 'Текст содержит больше 20000 строк')
    identities = []
    duration = json.loads(row['metadata']).get('duration')
    for index, line in enumerate(lines):
        try:
            uuid.UUID(line['line_id'])
        except (ValueError, KeyError, TypeError) as exc:
            raise media.MediaError('invalid_lyrics_document', 'Для каждой строки нужен устойчивый UUID') from exc
        identities.append(line['line_id'])
        if line.get('ordinal', index) != index or not isinstance(line.get('text'), str) or '\n' in line['text'] or '\r' in line['text']:
            raise media.MediaError('invalid_lyrics_document', 'Порядок и текст строк некорректны')
        stamp = line.get('start_ms')
        if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0 or duration and stamp > duration * 1000):
            raise media.MediaError('invalid_timing', 'Время строки выходит за границы аудио')
    if len(identities) != len(set(identities)) or len(render(lines).encode()) > media.MAX_LRC_BYTES:
        raise media.MediaError('invalid_lyrics_document', 'Повторные ID или слишком большой документ')


def enforce_locks(state, lines):
    indexed = {line['line_id']: line for line in lines}
    for identity, lock in state.get('locks', {}).items():
        line = indexed.get(identity)
        if line is None or lock.get('words_locked') and digest(line['text']) != lock['words_sha256'] or lock.get('timing_locked') and line['start_ms'] != lock['start_ms']:
            raise media.MediaError('lyrics_line_locked', 'Закрепленные слова или время строки изменены. Сначала снимите нужное закрепление')


def payload_document(conn, row, payload):
    state = ensure(conn, row)
    current = effective(conn, row, state)
    supplied = payload.get('lyrics_document')
    if supplied:
        lines = content(supplied['lines'])
        metadata = supplied.get('metadata', current['metadata'])
    else:
        parsed, metadata = parse(payload['lrc'])
        lines = remap(current['lines'], parsed, row['id'], digest(payload['lrc']))
    validate_lines(lines, row)
    enforce_locks(state, lines)
    return {**current, 'lines': lines, 'metadata': metadata, 'source': 'proposal', 'parse_error': None,
            **revisions(lines), 'lrc': render(lines)}


def check_payload(conn, row, payload, mode='manual'):
    if payload.get('lrc') is None:
        return payload
    result = deepcopy(payload)
    try:
        document = payload_document(conn, row, result)
    except media.MediaError:
        if mode != 'candidate':
            raise
        result.pop('lrc', None); result.pop('lyrics_document', None)
        result.get('field_sources', {}).pop('lrc', None)
        result.setdefault('policy_omitted_fields', []).append('lrc')
        return result
    if result.get('lyrics_document') and render(document['lines']) != result['lrc']:
        raise media.MediaError('lyrics_document_mismatch', 'LRC и структурированные строки отличаются')
    return result


def invalidate_alignment(conn, row, words_revision):
    found = conn.execute('SELECT payload FROM analyses WHERE track_id=?', (row['id'],)).fetchone()
    if not found:
        return
    data = json.loads(found['payload'])
    alignment = data.get('alignment')
    if alignment and alignment.get('words_revision') != words_revision:
        alignment.update(stale=True, stale_reason='lyrics_words_changed')
        # ASR is an independent observation and remains untouched.
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(data), row['id']))


def propose(conn, row, payload, edit=None):
    if payload.get('lrc') is None and edit is None:
        return payload
    state = ensure(conn, row)
    previous = effective(conn, row, state)
    result = deepcopy(payload)
    if edit is not None:
        edit = edit.model_dump() if hasattr(edit, 'model_dump') else edit
        if edit['base_version'] != state['version']:
            raise media.MediaError('lyrics_document_conflict', 'Документ изменен в другой сессии. Обновите карточку')
        lines = content(edit['lines'])
        validate_lines(lines, row); enforce_locks(state, lines)
        result['lrc'] = render(lines)
        result['lyrics_document'] = {**previous, 'lines': lines, 'metadata': edit.get('metadata') if edit.get('metadata') is not None else previous['metadata']}
    document = payload_document(conn, row, result)
    document['provenance'] = result.get('field_sources', {}).get('lrc', document.get('provenance', {'origin': 'owner_supplied'}))
    prior = {line['line_id']: line for line in previous['lines']}
    for line in document['lines']:
        old = prior.get(line['line_id'], {})
        line['words_state'] = old.get('words_state', 'raw') if old.get('text') == line['text'] else 'proposed'
        line['timing_state'] = old.get('timing_state', 'raw') if old.get('start_ms') == line['start_ms'] else 'proposed' if line['start_ms'] is not None else 'unaligned'
    state['version'] += 1
    conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(state), key(row['id'])))
    result['lyrics_document'] = {**document, 'version': state['version'], 'lines': document['lines'], **revisions(document['lines'])}
    result['lrc'] = render(document['lines'])
    if previous['words_revision'] != result['lyrics_document']['words_revision']:
        invalidate_alignment(conn, row, result['lyrics_document']['words_revision'])
    return result


def committed(conn, row, metadata, payload):
    state = ensure(conn, row)
    supplied = payload.get('lyrics_document')
    raw = metadata.get('lrc') or metadata.get('lyrics') or ''
    if supplied and payload.get('lrc') is not None:
        lines = content(supplied['lines'])
        enforce_locks(state, lines)
        for line in lines:
            line['words_state'] = 'accepted'
            line['timing_state'] = 'accepted' if line['start_ms'] is not None else 'unaligned'
    elif digest(raw) == state['embedded_sha256']:
        lines = content(state['lines'])
    else:
        try:
            lines, _ = parse(raw)
            lines = remap(state['lines'], lines, row['id'], digest(raw))
        except media.MediaError:
            lines = []  # Existing invalid lyrics must not break an unrelated tag commit.
    value = {**state, 'version': state['version'] + 1, 'file_revision': metadata['sha256'], 'embedded_sha256': digest(raw),
             'lines': lines, 'metadata': supplied.get('metadata', state['metadata']) if supplied else state['metadata'],
             'source': 'embedded', **revisions(lines)}
    conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(value), key(row['id'])))


def alignment_reference(document, original):
    parsed, _ = parse(original)
    lines = document['lines']
    if [line['text'] for line in parsed] != [line['text'] for line in lines]:
        # A partially timed draft still follows its durable ordinal, not a new
        # timestamp sort. Plain words carry that order to the aligner.
        lines = [line for line in lines if line['text'].strip()]
        original = '\n'.join(line['text'] for line in lines)
    return original, {'document_id': document['document_id'], 'words_revision': document['words_revision'],
                      'lines': [{'line_id': line['line_id'], 'original_ms': line['start_ms']} for line in lines]}


def analysis_view(row, analysis, document):
    if not analysis or not analysis.get('alignment'):
        return analysis
    aligned = analysis['alignment']
    asr = analysis.get('asr') or {}
    audio_identity = (json.loads(row['metadata']).get('audit') or {}).get('audio_identity')
    audio_current = analysis.get('input_sha256') == row['sha256'] or bool(audio_identity and audio_identity == asr.get('audio_identity'))
    words_current = aligned.get('words_revision') == document['words_revision']
    aligned['stale'] = not audio_current or not words_current or aligned.get('stale_reason') == 'alignment_occurrence_mismatch'
    if aligned['stale']:
        aligned['stale_reason'] = 'audio_revision_changed' if not audio_current else 'lyrics_words_changed'
    else:
        aligned.pop('stale_reason', None)
    return analysis


class LineEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    line_id: str = Field(min_length=36, max_length=36)
    ordinal: int = Field(ge=0, le=20000)
    text: str = Field(max_length=100000)
    start_ms: StrictInt | None = Field(default=None, ge=0)


class DocumentEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    lines: list[LineEdit] = Field(max_length=20000)
    metadata: dict[str, list[str]] | None = Field(default=None, max_length=1000)


class LineLock(BaseModel):
    model_config = ConfigDict(extra='forbid')
    line_id: str = Field(min_length=36, max_length=36)
    words_locked: bool
    timing_locked: bool


class LocksEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    lines: list[LineLock] = Field(max_length=20000)


def install(app, settings):
    @app.get('/api/tracks/{track_id}/lyrics-document')
    def get_document(request: Request, track_id: str):
        security.require_session(request, settings)
        with db.connect(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row:
                raise security.error(404, 'track_not_found', 'Трек не найден')
            return effective(conn, dict(row))

    @app.put('/api/tracks/{track_id}/lyrics-document/locks')
    def lock_lines(request: Request, track_id: str, body: LocksEdit):
        security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row:
                raise security.error(404, 'track_not_found', 'Трек не найден')
            row = dict(row)
            if request.headers.get('if-match', '').strip('"') != row['sha256']:
                raise security.error(412, 'revision_conflict', 'Файл изменился')
            state = ensure(conn, row); document = effective(conn, row, state)
            if body.base_version != state['version']:
                raise security.error(412, 'lyrics_document_conflict', 'Документ изменился в другой сессии')
            pending = conn.execute("SELECT 1 FROM jobs j,json_each(j.payload,'$.items') i WHERE j.kind IN ('apply','rollback','publish','rollback_ready') AND j.state IN ('queued','running','paused','waiting_authorization','interrupted') AND json_extract(i.value,'$.track_id')=? LIMIT 1", (track_id,)).fetchone()
            if pending:
                raise security.error(409, 'track_mutation_pending', 'Дождитесь завершения изменения файла')
            indexed = {line['line_id']: line for line in document['lines']}
            ids = [line.line_id for line in body.lines]
            if len(ids) != len(set(ids)) or set(ids) - set(indexed):
                raise security.error(422, 'invalid_lyrics_document', 'Неизвестные или повторные строки')
            locks = deepcopy(state['locks'])
            for item in body.lines:
                if not item.words_locked and not item.timing_locked:
                    locks.pop(item.line_id, None)
                else:
                    line = indexed[item.line_id]
                    locks[item.line_id] = {'words_locked': item.words_locked, 'words_sha256': digest(line['text']),
                                           'timing_locked': item.timing_locked, 'start_ms': line['start_ms']}
            state.update(version=state['version'] + 1, locks=locks)
            conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(state), key(track_id)))
            db.event(conn, 'lyrics_locks_changed', {'track_id': track_id, 'version': state['version'], 'line_ids': ids})
            return effective(conn, row, state)
