"""Compare timed candidates to independent ASR for one completed lyrics report."""
import hashlib
import json
import statistics
import uuid

from fastapi import Query, Request
from pydantic import BaseModel, Field

from . import (alignment, automatic_quality, catalog_sources, confirmed_fields, db,
               enrichment_gaps, field_policy, lyrics_review, media, preparation,
               review_queue, security, webhooks)
from .providers import matching_timed_lyrics

PAGE_SIZE = 60


class Selection(BaseModel):
    tokens: dict[str, str] = Field(min_length=1, max_length=PAGE_SIZE)


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def candidates(conn, row, analysis, retired, *, duration_only=False):
    meta = json.loads(row['metadata'])
    if (row['id'] in retired or row['status'] in {'error', 'missing', 'conflict'}
            or meta.get('input_error') or meta.get('format') == 'cue' or meta.get('cue_album_image')
            or any(analysis.get(k) for k in ('possible_mix', 'multiple_recordings', 'unresolved_conflicts', 'critical_conflicts'))
            or meta.get('lrc') or not enrichment_gaps.individual_music(meta, row['sha256'])
            or 'lrc' in field_policy.load(conn, row['id'])['locks']
            or lyrics_review.load(conn, row['id']).get('instrumental_confirmed')):
        return None
    options = [v for v in analysis.get('candidates', []) if isinstance(v,dict)
                   and v.get('provider') == 'lrclib' and isinstance(v.get('lrc'),str) and v['lrc']
                   and isinstance(v.get('match'),dict) and 'state' in v['match'] and 'instrumental_claim' in v
                   and confirmed_fields.permitted(conn, v, ('cache', 'derived_analysis'))]
    exact = sorted({v['lrc'] for v in options if matching_timed_lyrics(v)})
    if not duration_only:
        return exact
    if exact:
        return []
    return sorted({v['lrc'] for v in options if v['instrumental_claim'] is False and v['match']['state'] == 'needs_review'
                   and v['match'].get('reasons') == ['duration_differs']
                   and confirmed_fields.number(v['match'].get('duration_delta')) and v['match']['duration_delta'] > 2})


def language(texts):
    # Only set Russian when all qualifying references support it. Other languages use detection.
    return 'ru' if texts and all(review_queue.language_hint(s) == 'ru' for s in texts) else None


def compare(texts, asr, duration):
    words = asr.get('words') or [w for seg in asr.get('segments', []) for w in seg.get('words', [])]
    if not words:
        return {'state': 'checked_empty'}
    unscored = asr.get('engine') == 'gigaam' and asr.get('word_probabilities') is False
    choices = []
    for text in texts:
        try:
            result = alignment.align_lines(text, words, duration, unscored_words=unscored)
        except (ValueError, media.MediaError):
            continue
        lines = [line for line in result['lines'] if line['text'].strip()]
        delta = [abs(line['original_ms'] - line['start_ms']) for line in lines
                 if line['state'] == 'anchored' and line['original_ms'] is not None]
        if lines:
            choices.append((len(delta) / len(lines), -(statistics.median(delta) if delta else float('inf')),
                            len(delta), len(lines)))
    if not choices:
        return {'state': 'blocked'}
    coverage, negative_median, anchored, lines = max(choices)
    return {'state': 'matched' if coverage == 1 else 'partial', 'anchored': anchored,
            'lines': lines, 'median_ms': -negative_median if anchored else None}


def current_run(conn, settings, run_id, epoch):
    from .worker import root_identity
    run = preparation.load(conn)
    if (run.get('id') != run_id or not run.get('refines') or run.get('state') != 'review'
            or run.get('epoch') != epoch or run.get('root') != root_identity(settings.work_root)):
        raise security.error(409, 'preparation_stale', 'Откройте завершённый отчёт текущей папки.')
    return run


def page(conn, settings, run, offset, compare_words, *, duration_only=False):
    report_prefix = 'fill_report:' + run['report_id'] + ':'
    scan_prefix = 'fill_page:' + run['scan_id'] + ':'
    hits = conn.execute("""SELECT json_extract(r.value,'$[0]') AS id,json_extract(r.value,'$[1]') AS file,
        json_extract(s.value,'$[1]') AS revision FROM settings p JOIN json_each(p.value) r
        LEFT JOIN settings m ON m.key=?||substr(p.key,?)
        LEFT JOIN json_each(m.value) s ON json_extract(s.value,'$[0]')=json_extract(r.value,'$[0]')
        WHERE p.key GLOB ? AND json_extract(r.value,'$[3]')='lyrics_option_found'
        ORDER BY CAST(substr(p.key,?) AS INTEGER),r.key LIMIT ? OFFSET ?""",
        (scan_prefix, len(report_prefix)+1, report_prefix+'*', len(report_prefix)+1, PAGE_SIZE+1, offset)).fetchall()
    retired = catalog_sources.completed_sources(conn)
    busy = confirmed_fields.active_tracks(conn)
    items = []
    for hit in hits[:PAGE_SIZE]:
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1 AND sha256=?',
                           (hit['id'], hit['revision'])).fetchone()
        item = {'id': hit['id'], 'file': hit['file'], 'state': 'changed', 'variants': 0}
        if not duration_only:
            items.append(item)
        if not row:
            continue
        saved = conn.execute('SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?',
                             (row['id'], row['sha256'])).fetchone()
        analysis = json.loads(saved[0]) if saved else {}
        texts = candidates(conn, row, analysis, retired, duration_only=duration_only)
        if not texts:
            item['state'] = 'blocked' if texts is None else 'no_match'
            continue
        if duration_only:
            items.append(item)
        hint = language(texts)
        item.update(variants=len(texts), language=hint)
        asr = analysis.get('asr') or {}
        if (automatic_quality.inference_bound(conn, row, asr)
                and (not hint or (asr.get('language') == 'ru' and asr.get('engine') == 'gigaam'))):
            item.update(compare(texts, asr, json.loads(row['metadata'])['duration']) if compare_words else {'state':'checked'})
            continue
        token = selection_token(row, hint, texts, duration_only)
        receipt = review_queue.receipt_key('audio-check', token)
        if row['id'] in busy:
            item['state'] = 'busy'
        elif conn.execute('SELECT 1 FROM settings WHERE key=?', (receipt,)).fetchone():
            item['state'] = 'attempted'
        elif not settings.inference_url:
            item['state'] = 'blocked'
        else:
            item.update(state='ready', token=token)
    return {'items': items, 'offset': offset, 'next_offset': offset+PAGE_SIZE if len(hits)>PAGE_SIZE else None}


def folder_references(conn, row, analysis, retired):
    timed = candidates(conn, row, analysis, retired)
    if timed is None or timed:
        return timed, False
    plain = {c['plain_lyrics'] for c in analysis.get('candidates', []) if isinstance(c, dict)
             and c.get('provider') == 'lrclib' and c.get('instrumental_claim') is False
             and isinstance(c.get('plain_lyrics'), str) and 0 < len(c['plain_lyrics'].strip()) <= 100000
             and (c.get('match') or {}).get('state') == 'matching_metadata'
             and confirmed_fields.permitted(conn, c, ('cache', 'derived_analysis'))}
    return (sorted(plain), True) if len(plain) == 1 else ([], False)


def folder_check(conn, row, retired):
    """Return a revision-bound check for a timed reference in any scanned track."""
    saved = conn.execute('SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?',
                         (row['id'], row['sha256'])).fetchone()
    if not saved:
        return None
    analysis = json.loads(saved[0])
    texts, plain = folder_references(conn, row, analysis, retired)
    if not texts:
        return None
    hint = language(texts)
    asr = analysis.get('asr') or {}
    if automatic_quality.inference_bound(conn, row, asr) and (
            not hint or asr.get('language') == 'ru' and asr.get('engine') == 'gigaam'):
        if plain:
            words = asr.get('words') or [w for seg in asr.get('segments', []) for w in seg.get('words', [])]
            unscored = asr.get('engine') == 'gigaam' and asr.get('word_probabilities') is False
            try:
                result = alignment.align_lines(texts[0], words, json.loads(row['metadata'])['duration'], unscored_words=unscored)
            except (ValueError, media.MediaError):
                return {'result': 'audio_blocked'}
            return {'result': 'audio_matched' if result['complete'] else 'audio_partial'}
        result = compare(texts, asr, json.loads(row['metadata'])['duration'])
        return {'result': 'audio_' + result['state']}
    token = selection_token(row, hint, texts, False, plain=plain)
    if conn.execute('SELECT 1 FROM settings WHERE key=?', (review_queue.receipt_key('audio-check', token),)).fetchone():
        return {'result': 'audio_previously_attempted'}
    # An earlier failed/cancelled or unaccounted attempt on this revision needs inspection.
    prior = conn.execute("""SELECT 1 FROM jobs j JOIN json_each(j.payload,'$.items') i
        LEFT JOIN job_items ji ON ji.job_id=j.id AND ji.item_key=?
        WHERE j.kind IN ('align','transcribe') AND json_extract(i.value,'$.track_id')=?
          AND json_extract(i.value,'$.sha256')=? AND COALESCE(ji.state,'')<>'succeeded' LIMIT 1""",
        (row['id'], row['id'], row['sha256'])).fetchone()
    if prior:
        return {'result': 'audio_previously_attempted'}
    return {'audio_check_token': token, 'language_hint': hint, **({'audio_check_plain': True} if plain else {})}


def selection_token(row, hint, texts, duration_only, *, plain=False):
    evidence = [row['id'], row['sha256'], hint, texts]
    if plain:
        evidence.append('plain_reference')
    if duration_only:
        evidence.append('duration_differs')
    return digest(evidence)


def install(app, settings):
    @app.get('/api/preparation/{run_id}/audio-check')
    def preview(request: Request, run_id: uuid.UUID, offset: int = Query(0, ge=0), duration_only: bool = False):
        active = security.require_session(request, settings, owner_only=True)
        with db.connect(settings.db_path) as conn:
            conn.execute('BEGIN')
            run = current_run(conn, settings, str(run_id), active['auth_epoch'])
            return page(conn, settings, run, offset, True, duration_only=duration_only)

    @app.post('/api/preparation/{run_id}/audio-check', status_code=202)
    def start(request: Request, run_id: uuid.UUID, body: Selection, offset: int = Query(0, ge=0), duration_only: bool = False):
        active = security.require_session(request, settings, True, owner_only=True)
        request_key = [offset, sorted(body.tokens.items())]
        if duration_only:
            request_key.append('duration_differs')
        key = str(uuid.uuid5(run_id, digest(request_key)))
        with db.transaction(settings.db_path) as conn:
            run = current_run(conn, settings, str(run_id), active['auth_epoch'])
            prior = conn.execute('SELECT total FROM jobs WHERE id=? AND auth_epoch=?', (key, run['epoch'])).fetchone()
            if prior:
                return {'job_id': key, 'total': prior['total']}
            if not settings.inference_url or not webhooks.can_accept(conn) or conn.execute(
                    "SELECT 1 FROM settings WHERE key='recovery_required' AND value='true'").fetchone():
                raise security.error(409, 'preparation_unavailable', 'Проверьте доступность обработки в настройках.')
            view = {v['id']:v for v in page(conn, settings, run, offset, False, duration_only=duration_only)['items'] if v['state']=='ready'}
            if any(tid not in view or view[tid]['token'] != token for tid, token in body.tokens.items()):
                raise security.error(409, 'review_plan_stale', 'Состояние записей изменилось. Обновите результат.')
            items = []
            for tid, token in body.tokens.items():
                row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
                item = {'track_id':tid, 'sha256':row['sha256'], 'relative_path':row['relative_path'],
                        'language_hint':view[tid]['language'], 'audio_check_token':token}
                if duration_only:
                    item['audio_check_duration_only'] = True
                review_queue.freeze_inference(conn, row, item, 'transcribe', {}, False)
                items.append(item)
                conn.execute('INSERT INTO settings VALUES(?,?)',
                             (review_queue.receipt_key('audio-check', token), db.encode({'job_id':key})))
            now = db.utcnow()
            payload = {'items':items, 'params':{}}
            from .tracing import carrier
            if carrier():
                payload['_trace'] = carrier()
            conn.execute("""INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch)
                VALUES(?,'transcribe','queued','pending',?,?,?,?,?)""",
                (key,db.encode(payload),len(items),now,now,active['auth_epoch']))
            db.event(conn, 'job_created', {'job_id':key, 'kind':'transcribe'})
            return {'job_id':key,'total':len(items)}


def recheck(conn, row, item):
    saved = conn.execute('SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?',
                         (row['id'], row['sha256'])).fetchone()
    duration_only = item.get('audio_check_duration_only') is True
    analysis = json.loads(saved[0]) if saved else {}
    retired = catalog_sources.completed_sources(conn)
    plain = item.get('audio_check_plain') is True
    if plain:
        texts, still_plain = folder_references(conn, row, analysis, retired)
        if not still_plain or duration_only:
            texts = None
    else:
        texts = candidates(conn, row, analysis, retired, duration_only=duration_only)
    if not texts or selection_token(row, language(texts), texts, duration_only, plain=plain) != item['audio_check_token']:
        raise media.MediaError('review_plan_stale', 'Данные записи или разрешения источника изменились.')
