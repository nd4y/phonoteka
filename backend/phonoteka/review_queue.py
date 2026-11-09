"""One read model for review counts, filters and bounded draft preparation.

No proposal is deleted and no audio is written. A preparation receipt survives
refresh/restart so the same evidence cannot silently spend another ASR/quota run.
"""
import hashlib
import json
from collections import Counter

from . import catalog_sources, db, field_policy, media, recording_review, provider_attempts, provider_policy

CATEGORIES = ('prepare', 'choose', 'listen', 'file')
FILTERS = {'review', *(f'review_{name}' for name in CATEGORIES)}
PREFIX = 'review_prepare:'
ACTIVE = {'queued', 'running', 'paused', 'waiting_authorization', 'waiting', 'waiting_provider', 'deferred', 'interrupted'}


def proposal_state(meta, revision, proposal):
    if not proposal: return 'none'
    if proposal['input_sha256'] != revision: return 'stale'
    p = proposal['data']
    # Artwork and explicit slot changes need inspection; never infer equal bytes
    # from has_art, or load base64 artwork into the polling read model.
    if p.get('cover_present') or p.get('slot_selection'): return 'pending'
    if any(field_policy.normalized_value(k, v) != field_policy.normalized_value(k, meta.get('tags', {}).get(k))
           for k, v in p.get('tags', {}).items()): return 'pending'
    if 'lrc' in p and p['lrc'] is not None and p['lrc'] != (meta.get('lrc') or meta.get('lyrics') or ''):
        return 'pending'
    return 'no_changes'


def receipt_key(kind, token):
    return PREFIX + kind + ':' + token


def project(conn, settings, ids=None):
    completed = catalog_sources.completed_sources(conn)
    where = ' AND t.id IN (SELECT value FROM json_each(?))' if ids is not None else ''
    rows = conn.execute('''SELECT t.id,t.sha256,t.status,
        json_object('tags',json(COALESCE(json_extract(t.metadata,'$.tags'),'{}')),
          'lrc',json_extract(t.metadata,'$.lrc'),'lyrics',json_extract(t.metadata,'$.lyrics'),
          'usage_provenance',json(COALESCE(json_extract(t.metadata,'$.usage_provenance'),'{}')),
          'recording_classification',json(COALESCE(json_extract(t.metadata,'$.recording_classification'),'{}')),
          'possible_mix',json(CASE WHEN json_extract(t.metadata,'$.possible_mix')=1 THEN 'true' ELSE 'false' END),
          'format',json_extract(t.metadata,'$.format'),'cue_album_image',json_extract(t.metadata,'$.cue_album_image'),
          'input_error',json_extract(t.metadata,'$.input_error'),
          'cue',json_object('errors',json_extract(t.metadata,'$.cue.errors')),
          'audit',json_object('audio_identity',json_extract(t.metadata,'$.audit.audio_identity'))) AS metadata,
        p.id AS proposal_id,p.input_sha256 AS proposal_revision,
        json_extract(p.payload,'$.tags') AS proposal_tags,
        json_type(p.payload,'$.lrc') AS lrc_type,json_extract(p.payload,'$.lrc') AS proposal_lrc,
        COALESCE(length(json_extract(p.payload,'$.cover'))>0,0) AS cover_present,
        json_extract(p.payload,'$.slot_selection') AS slots,
        json_extract(p.payload,'$.field_sources') AS sources_by_field,
        json_extract(p.payload,'$.sources') AS sources,
        json_extract(f.value,'$.version') AS field_version
        FROM tracks t LEFT JOIN proposals p ON p.id=(SELECT id FROM proposals
            WHERE track_id=t.id ORDER BY created_at DESC,rowid DESC LIMIT 1)
        LEFT JOIN settings f ON f.key='track_field_policy:'||t.id
        WHERE t.is_current=1''' + where, (json.dumps(ids),) if ids is not None else ())
    receipts = {r['key']: {'id': r['id'], 'state': r['state']} for r in conn.execute('''
        SELECT s.key,j.id,j.state FROM settings s JOIN jobs j ON j.id=json_extract(s.value,'$.job_id')
        WHERE s.key LIKE 'review_prepare:%' ''')}
    recovery = conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()
    result = {}
    for row in rows:
        meta = json.loads(row['metadata']); p = None
        if row['proposal_id']:
            data = {'tags': json.loads(row['proposal_tags'] or '{}'), 'cover_present': bool(row['cover_present']),
                    'slot_selection': json.loads(row['slots'] or 'null')}
            if row['lrc_type'] is not None: data['lrc'] = row['proposal_lrc']
            data['field_sources'] = json.loads(row['sources_by_field'] or '{}')
            data['sources'] = json.loads(row['sources'] or '[]')
            p = {'id': row['proposal_id'], 'input_sha256': row['proposal_revision'], 'data': data}
        state = proposal_state(meta, row['sha256'], p)
        value = {'category': None, 'proposal_state': state, 'message': '', 'action': None, 'preparation_job': None}
        def reason(category, code, message, kind=None):
            value.update(category=category, reason=code, message=message)
            if kind:
                token = hashlib.sha256(db.encode([row['id'], row['sha256'],
                    row['proposal_id'] if state == 'pending' else None,
                    row['field_version'], meta.get('recording_classification'), kind]).encode()).hexdigest()
                receipt = receipts.get(receipt_key(kind, token))
                if receipt:
                    value['preparation_job'] = receipt
                    value['category'] = 'prepare' if receipt['state'] in ACTIVE else 'listen' if kind in {'align', 'recognize'} else 'choose'
                    value['message'] = ('Подготовка выполняется. Результат появится в этой карточке.' if receipt['state'] in ACTIVE else
                        'Подготовка отменена. Повтор можно запустить из карточки вручную.' if receipt['state'].startswith('cancelled') else
                        'Подготовка завершилась с ошибками. Откройте обработку и проверьте причину.' if receipt['state'] in {'failed', 'completed_with_errors'} else
                        'Подготовка завершена. Проверьте результат; повтор автоматически не запускается.')
                elif not (recovery and recovery['value'] == 'true'):
                    value['action'] = {'kind': kind, 'token': token}
        if row['id'] in completed:
            pass
        elif row['status'] in {'error', 'missing', 'conflict'} or meta.get('input_error') or meta.get('cue', {}).get('errors'):
            reason('file', 'file_problem', 'Проверьте доступность и состояние исходного файла.')
        elif recording_review.possible_mix(meta, row['sha256']):
            owner = recording_review.decision(meta, row['sha256']).get('origin') == 'owner'
            kind = 'recognize' if settings.external_fingerprints and not owner and meta.get('format') != 'cue' and not meta.get('cue_album_image') else None
            identity = (meta.get('audit') or {}).get('audio_identity')
            usage = conn.execute('SELECT value FROM settings WHERE key=?', (provider_attempts.RECOGNITION_PREFIX + str(identity),)).fetchone() if identity else None
            remaining = max(0, provider_attempts.RECOGNITION_CALL_LIMIT - (json.loads(usage['value']).get('calls', 0) if usage else 0))
            if not remaining: kind = None
            reason('prepare' if kind else 'listen', 'possible_mix',
                   'Сверим разнесённые фрагменты в пределах оставшегося лимита. Спорный состав останется на проверке.' if kind else
                   'Лимит распознавания использован. Сравните сохранённые результаты и прослушайте запись.' if not remaining else
                   'Прослушайте запись и уточните, одна ли это композиция.', kind)
            if value['action']: value['action']['maximum_requests'] = remaining
        elif state == 'pending':
            text = p['data'].get('lrc'); untimed = False
            if text:
                try: untimed = not media.parse_lrc(text)['complete']
                except media.MediaError:
                    reason('listen', 'invalid_lyrics', 'В тексте повреждены таймкоды. Исправьте их перед выравниванием.')
            if not value['category']:
                kind = 'align' if untimed and settings.inference_url else None
                denied = False
                if kind:
                    try:
                        reference = provider_policy.alignment_payload(conn, row, text, p['data'])
                        provider_policy.require_payload(conn, row, reference, 'derived_analysis')
                    except media.MediaError:
                        denied = True; kind = None
                reason('prepare' if kind else 'listen' if untimed else 'choose',
                       'plain_lyrics' if untimed else 'proposal',
                       'Подготовим таймкоды по аудио. Слова и времена останутся на проверке.' if kind else
                       'Разрешение источника не позволяет выравнивать этот текст. Обновите источник или проверьте настройки.' if denied else
                       'Проверьте найденный текст и настройте ASR для выравнивания.' if untimed else
                       'Сравните сохранённое предложение с данными файла.', kind)
        elif row['status'] == 'needs_review' and state != 'no_changes':
            reason('choose', 'unresolved', 'Проверьте причины пропусков и доступные источники.')
        result[row['id']] = value
    return result


def counts(values):
    counted = Counter(v['category'] for v in values.values() if v['category'])
    return {'total': sum(counted.values()), **{name: counted[name] for name in CATEGORIES}}


def selection(values, filter):
    category = filter.removeprefix('review_') if filter != 'review' else None
    return [key for key, value in values.items() if value['category'] and (category is None or value['category'] == category)]


def language_hint(text):
    letters = [char for char in text if char.isalpha()]
    if letters and sum('а' <= c.lower() <= 'я' or c.lower() == 'ё' for c in letters) / len(letters) > .6:
        return 'ru'
    return None  # Latin script alone does not establish English.


def plan(conn, settings):
    values = project(conn, settings)
    batches = []
    for kind in ('recognize', 'align'):
        selected = [
            (tid, v) for tid, v in values.items() if v.get('action') and v['action']['kind'] == kind]
        batches.append({'kind': kind, 'total': len(selected), 'items': [
            {'track_id': tid, 'token': v['action']['token']} for tid, v in selected[:200]]})
    # Existing verified release groups can enter the existing album batch flow.
    from . import release_review
    albums = []
    for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'release_review:%'"):
        stored = json.loads(row['value'])
        report = release_review.report(conn, stored)
        ids = report.get('matched_track_ids', [])
        if report.get('state') in {'complete', 'subset_complete'} and 1 <= len(ids) <= 200 and any(t in values and values[t]['category'] for t in ids):
            albums.append({'id': stored['id'], 'version': stored['version'], 'track_ids': ids,
                           'title': (report.get('reference') or {}).get('title') or 'Подтверждённый релиз', 'count': len(ids)})
    return {'counts': counts(values), 'batches': batches, 'albums': albums, 'automatic_apply': False}
