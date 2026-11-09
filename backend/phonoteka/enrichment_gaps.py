"""Explain missing fields from current evidence, without fetching provider data.

Catalog pages project candidate flags in SQLite. Cover bytes, lyrics and ASR
transcripts must not be copied into this read model or accumulated for a page.
"""
import json

from . import acoustid, media, provider_attempts, provider_policy, recording_review
from .providers import scalar


FIELDS = ('title', 'artist', 'album', 'cover', 'synced_lyrics')
MUSIC_GAP_FILTERS = {
    'music_gaps': FIELDS,
    'music_missing_tags': FIELDS[:3],
    'music_missing_art': ('cover',),
    'music_missing_lyrics': ('synced_lyrics',),
}
SPOKEN_GENRES = {
    'audiobook', 'podcast', 'speech', 'spoken word',
    'аудиокнига', 'подкаст', 'разговорная запись',
}
RETRY_STATES = {
    'provider_cooldown', 'provider_unavailable', 'rate_limited', 'budget_exhausted',
    'timeout', 'network_error', 'deadline_exceeded', 'partial',
}
CONFIG_STATES = {'key_required', 'provider_unauthorized', 'disabled', 'provider_disabled'}


def project(conn, ids):
    if not ids:
        return {}
    query = '''SELECT a.track_id,a.input_sha256,a.updated_at,
        json_extract(a.payload,'$.sources') AS sources,
        json_extract(a.payload,'$.alignment.coverage') AS alignment_coverage,
        (SELECT json_group_array(json_object(
            'provider',json_extract(c.value,'$.provider'),
            'kind',json_extract(c.value,'$.kind'),
            'match',json_extract(c.value,'$.match'),
            'usage',json_extract(c.value,'$.usage'),
            'transient',COALESCE(json_extract(c.value,'$.transient_only'),0),
            'title',COALESCE(length(json_extract(c.value,'$.tags.title'))>0,0),
            'artist',COALESCE(length(json_extract(c.value,'$.tags.artist'))>0,0),
            'album',COALESCE(length(json_extract(c.value,'$.tags.album'))>0,0),
            'releases',COALESCE(json_array_length(c.value,'$.releases'),0),
            'cover',COALESCE(length(json_extract(c.value,'$.cover'))>0,0),
            'synced_lyrics',COALESCE(length(json_extract(c.value,'$.lrc'))>0,0),
            'plain',COALESCE(length(json_extract(c.value,'$.plain_lyrics'))>0,0),
            'instrumental',COALESCE(json_extract(c.value,'$.instrumental_claim'),0)
        )) FROM json_each(a.payload,'$.candidates') c) AS candidates
        FROM analyses a WHERE a.track_id IN (%s)''' % ','.join('?' for _ in ids)
    result = {r['track_id']: {**dict(r), 'sources': json.loads(r['sources'] or '[]'),
                            'candidates': json.loads(r['candidates'] or '[]')}
              for r in conn.execute(query, ids)}
    # A failed search leaves the previous analysis intact. Its old source flags
    # must not claim that a newer lookup never ran. Read only opaque job facts;
    # never project the frozen payload, error text or provider response bodies.
    attempts = '''SELECT ji.item_key AS track_id,j.id,j.kind,j.created_at,j.updated_at
        FROM jobs j JOIN job_items ji ON ji.job_id=j.id
        JOIN tracks t ON t.id=ji.item_key AND t.is_current=1
        WHERE j.kind IN ('analyze','recognize') AND ji.state='failed'
          AND ji.item_key IN (%s)
          AND EXISTS (SELECT 1 FROM json_each(j.payload,'$.items') frozen
              WHERE json_extract(frozen.value,'$.track_id')=t.id
                AND json_extract(frozen.value,'$.sha256')=t.sha256)
        ORDER BY j.created_at DESC,j.id DESC''' % ','.join('?' for _ in ids)
    for attempt in conn.execute(attempts, ids):
        value = result.setdefault(attempt['track_id'], {
            'input_sha256': None, 'updated_at': None, 'sources': [], 'candidates': []})
        # updated_at belongs to the entire batch: another item may advance it
        # after this track has already received a successful replacement analysis.
        if value.get('last_failed_search') or (value['updated_at'] and
                                             value['updated_at'] >= attempt['created_at']):
            continue
        value['last_failed_search'] = dict(attempt)
    return result


def missing_fields(meta):
    if meta.get('format') == 'cue' or meta.get('cue_album_image'):
        return []
    tags = meta.get('tags', {})
    return [field for field in FIELDS if not (
        meta.get('has_art') if field == 'cover' else
        meta.get('lrc') if field == 'synced_lyrics' else scalar(tags, field).strip())]


def matches_music_gap(metadata, revision, filter_name):
    meta = json.loads(metadata)
    gaps = missing_fields(meta)
    if not any(field in gaps for field in MUSIC_GAP_FILTERS[filter_name]):
        return False
    if recording_review.possible_mix(meta, revision):
        return False
    genre = meta.get('tags', {}).get('genre')
    genres = genre if isinstance(genre, list) else [genre]
    return not any(str(value).strip().casefold() in SPOKEN_GENRES for value in genres)


def describe(conn, settings, row, *, analysis=None, proposal=None, connection=None):
    meta = json.loads(row['metadata'])
    missing = missing_fields(meta)
    if not missing:
        return []
    if connection is None:
        connection = acoustid.public_connection(settings)
    current = bool(analysis and analysis['input_sha256'] == row['sha256'])
    checked_at = analysis['updated_at'] if analysis else None
    candidates = analysis['candidates'] if current else []
    sources = {s.get('provider'): s.get('state') for s in analysis['sources']} if current else {}
    draft = json.loads(proposal['payload']) if proposal and proposal['input_sha256'] == row['sha256'] else {}
    draft = provider_policy.redact_payload(conn, draft) if draft else {}
    result = []
    for field in missing:
        def add(code, message, action, next_step, provider=None, count=None):
            identity = (meta.get('audit') or {}).get('audio_identity')
            if action == 'recognize' and isinstance(identity, str):
                saved = conn.execute('SELECT value FROM settings WHERE key=?',
                                     (provider_attempts.RECOGNITION_PREFIX + identity,)).fetchone()
                if saved and json.loads(saved['value']).get('calls', 0) >= provider_attempts.RECOGNITION_CALL_LIMIT:
                    code, message, action, next_step = (
                        'recognition_limit', 'Достигнут предел проверок этой аудиозаписи по отпечатку.', 'sources',
                        'Новая очередь не сбрасывает этот предел. Проверьте сохранённые варианты и уточните данные записи.')
            result.append({'field': field, 'code': code, 'message': message, 'action': action,
                           'next_step': next_step, 'provider': provider,
                           'checked_at': checked_at, 'analysis_current': current,
                           'candidate_count': count})
            coverage = analysis.get('alignment_coverage') if analysis else None
            if field == 'synced_lyrics' and type(coverage) in (int, float) and 0 <= coverage <= 1:
                result[-1]['last_alignment_coverage'] = coverage

        if row['status'] == 'missing':
            add('file_missing', 'Файл недоступен в подключённой папке.', 'sources', 'Проверьте папку и повторите сканирование.')
            continue
        if meta.get('input_error'):
            add('invalid_audio', 'Аудио не удалось прочитать.', 'sources', 'Проверьте исходный файл или создайте читаемую копию, если звук декодируется.')
            continue
        if recording_review.possible_mix(meta, row['sha256']):
            add('recording_unconfirmed', 'Возможно, это несколько композиций в одном файле.', 'sources', 'Прослушайте запись и уточните её тип перед применением данных.')
            continue
        if not meta.get('write_supported', meta.get('format') in {'mp3', 'flac'}):
            add('format_requires_preparation', 'В этот формат нельзя записать данные напрямую.', 'sources', 'Создайте редактируемую копию в разделе источников.')
            continue
        draft_value = draft.get('lrc' if field == 'synced_lyrics' else 'cover') if field in {'synced_lyrics', 'cover'} else scalar(draft.get('tags', {}), field).strip()
        if field == 'synced_lyrics' and draft_value:
            try:
                draft_value = media.parse_lrc(draft_value)['complete']
            except media.MediaError:
                draft_value = False
        if draft_value:
            add('proposal_ready', 'Есть сохранённый вариант, ещё не записанный в файл.', 'review', 'Проверьте предложение и запишите изменения.')
            continue
        failed_search = analysis.get('last_failed_search') if analysis else None
        if failed_search:
            recognize = failed_search['kind'] == 'recognize'
            retained = 'Прежние результаты поиска сохранены. ' if analysis.get('updated_at') else ''
            add('search_failed', 'Последняя попытка распознавания завершилась ошибкой.' if recognize else
                'Последняя попытка поиска завершилась ошибкой.',
                'sources' if recognize else 'search',
                retained + ('Проверьте ошибку в обработке перед повторным распознаванием.' if recognize else
                            'Проверьте ошибку в обработке и повторите поиск.'))
            result[-1]['checked_at'] = failed_search['updated_at']
            result[-1]['failed_job_id'] = failed_search['id']
            continue
        if field == 'synced_lyrics' and meta.get('lyrics') and not analysis:
            add('plain_lyrics_only', 'В файле есть текст без таймкодов.', 'lyrics', 'Проверьте текст и запустите выравнивание с аудио.')
            continue
        if not analysis or not current:
            add('search_stale' if analysis else 'not_searched',
                'Результат поиска относится к прежней версии файла.' if analysis else 'Поиск для этой записи ещё не выполнялся.',
                'search', 'Запустите поиск для текущей версии файла.')
            continue
        relevant_kinds = {'cover'} if field == 'cover' else {'lyrics'} if field == 'synced_lyrics' else {'metadata', 'release'}
        relevant = [c for c in candidates if c.get('kind') in relevant_kinds]
        usable = [c for c in relevant if not c.get('transient') and
                  (not c.get('usage') or (provider_policy._allowed(conn, c['usage'], 'cache') and
                                         provider_policy._allowed(conn, c['usage'], 'embed')))]
        filled = [c for c in usable if c.get(field)]
        if filled:
            reasons = {r for c in filled for r in (c.get('match') or {}).get('reasons', [])}
            code = 'timing_requires_review' if field == 'synced_lyrics' and reasons & {'invalid_lyrics', 'lyrics_out_of_range'} else 'candidates_require_review'
            message = 'В найденном тексте есть ошибки таймкодов.' if code == 'timing_requires_review' else 'Найдены варианты, но подходящий ещё не выбран.'
            add(code, message, 'sources', 'Сравните варианты источников с этой записью.', count=len(filled))
            continue
        if relevant and not usable:
            kinds = 'cover' if field == 'cover' else 'lyrics' if field == 'synced_lyrics' else 'metadata'
            can_refresh = any(provider_policy.rule(conn, c.get('provider'), kinds).get('allow_cache') and
                              provider_policy.rule(conn, c.get('provider'), kinds).get('allow_embed') for c in relevant)
            fingerprint_only = all(c.get('provider') in {'shazam', 'acoustid'} for c in relevant)
            add('results_expired' if can_refresh else 'source_policy_restricted',
                'Сохранённые результаты больше нельзя применить; нужен новый поиск.' if can_refresh else 'Настройки источника запрещают сохранение или запись этих данных.',
                ('recognize' if fingerprint_only else 'search') if can_refresh else 'settings',
                'Обновите результаты поиска.' if can_refresh else 'Проверьте разрешённые операции источника в настройках.')
            continue
        providers = ('coverartarchive',) if field == 'cover' else ('lrclib',) if field == 'synced_lyrics' else ('musicbrainz', 'acoustid', 'shazam')
        exhausted = next((p for p in providers if sources.get(p) == 'recognition_attempt_budget_exhausted'), None)
        if exhausted:
            add('recognition_limit', 'Достигнут предел проверок этой аудиозаписи по отпечатку.', 'sources',
                'Новая очередь не сбрасывает этот предел. Проверьте сохранённые варианты и уточните данные записи.', exhausted)
            continue
        unknown = next((p for p in providers if sources.get(p) == 'unknown_outcome'), None)
        if unknown:
            add('provider_outcome_unknown', 'Источник не подтвердил результат отправленного запроса.', 'sources',
                'Проверьте сохранившиеся результаты; этот ответ не означает, что запись не найдена.', unknown)
            continue
        failed = next((p for p in providers if sources.get(p) in RETRY_STATES), None)
        if failed:
            add('provider_retry', 'Поиск не завершён: источник временно недоступен или запрос отложен.', 'search', 'Повторите поиск после окончания паузы источника.', failed)
            continue
        if field == 'synced_lyrics':
            if meta.get('lyrics') or any(c.get('plain') for c in usable):
                add('plain_lyrics_only', 'Найден текст без таймкодов.', 'sources', 'Проверьте текст, затем выровняйте его с аудио.', 'lrclib')
                continue
            if any(c.get('instrumental') for c in usable):
                add('instrumental_unconfirmed', 'Источник пометил вариант как инструментальный. Это ещё не подтверждено.', 'sources', 'Прослушайте запись и проверьте совпадение; текст не исключён автоматически.', 'lrclib')
                continue
        if field == 'album' and any(c.get('releases') for c in usable):
            add('release_unselected', 'Для записи найдены издания, но альбом ещё не выбран.', 'sources', 'Проверьте альбом и состав издания в вариантах источников.', 'musicbrainz')
            continue
        if field == 'cover' and not sources.get('coverartarchive'):
            if sources.get('musicbrainz') in RETRY_STATES:
                add('provider_retry', 'Поиск обложки не начат: не завершён поиск издания.', 'search', 'Повторите поиск метаданных после окончания паузы источника.', 'musicbrainz')
                continue
            add('cover_not_searched', 'Отдельный поиск обложки ещё не выполнялся.', 'sources', 'Проверьте альбом и варианты изданий, затем повторите поиск.', 'coverartarchive')
            continue
        restricted = next((p for p in providers if sources.get(p) == 'provider_policy_denied'), None)
        if restricted:
            add('source_policy_restricted', 'Запрос запрещён настройками использования источника.', 'settings', 'Проверьте разрешённые операции источника в настройках.', restricted)
            continue
        configured = next((p for p in providers if sources.get(p) in CONFIG_STATES), None)
        if configured:
            refreshed = configured == 'acoustid' and connection['configured']
            add('connection_now_ready' if refreshed else 'connection_required',
                'AcoustID уже подключён; прошлый поиск выполнялся без доступа.' if refreshed else 'Один из источников не подключён или отклонил доступ.',
                'recognize' if refreshed else 'settings',
                'Повторите распознавание с текущим подключением.' if refreshed else 'Проверьте подключение источника в настройках.', configured)
            continue
        if any(sources.get(p) == 'insufficient_metadata' for p in providers):
            add('identity_required', 'Для поиска не хватает названия или исполнителя.', 'recognize', 'Сначала распознайте запись по аудиоотпечатку.')
            continue
        if any(sources.get(p) == 'no_match' for p in providers):
            add('no_result_for_query', 'Последний поиск не дал подходящих данных для этого поля.', 'sources', 'Проверьте название и исполнителя, попробуйте другой вариант или источник.')
            continue
        add('field_not_returned', 'Поиск не дал значения для этого поля; причина не установлена.', 'sources', 'Откройте результаты источников и проверьте исходные данные.')
    return result


def for_page(conn, settings, rows, proposals):
    analyses = project(conn, [r['id'] for r in rows])
    connection = acoustid.public_connection(settings)
    return {r['id']: describe(conn, settings, r, analysis=analyses.get(r['id']),
                              proposal=proposals.get(r['id']), connection=connection) for r in rows}
