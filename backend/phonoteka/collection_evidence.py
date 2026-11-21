"""Revision-bound checks for the saved folder manifest. No audio is sent to a catalog."""
import json
import uuid

from fastapi import Query, Request

from . import (audio_check, automatic_quality, catalog_sources, confirmed_fields as fields, db,
               enrichment_gaps, field_policy, lyrics_review, media, preparation, provider_policy,
               security, webhooks)
from .providers import Providers, ProviderError, embedded_recording_id, musicbrainz_candidate, normalized, scalar

KIND = 'collection_evidence'
PROFILE = 'collection-evidence-v1'
INSTRUMENTAL = 'c031ed4f-c9bb-4394-8cf5-e8ce4db512ae'


def review_id(run_id):
    return str(uuid.uuid5(uuid.UUID(str(run_id)), PROFILE))


def scope(conn, row, meta, analysis):
    missing = enrichment_gaps.missing_fields(meta)
    if (row['status'] in {'missing', 'conflict', 'error'} or meta.get('input_error')
            or meta.get('cue_album_image') or meta.get('format') == 'cue'
            or not enrichment_gaps.individual_music(meta, row['sha256'])
            or any(analysis.get(k) for k in ('possible_mix', 'multiple_recordings', 'critical_conflicts', 'unresolved_conflicts'))):
        return None
    if 'cover' in missing:
        return 'cover'
    if missing != ['synced_lyrics']:
        return None
    options = [c for c in analysis.get('candidates', []) if c.get('provider') == 'lrclib']
    exact = [c for c in options if (c.get('match') or {}).get('state') == 'matching_metadata']
    if any(c.get('lrc') for c in exact):
        return 'timing'
    if (exact and all(c.get('instrumental_claim') is True for c in exact)
            and not any(c.get('lrc') or c.get('plain_lyrics') for c in options)):
        return 'instrumental'
    return None


def bound_recording(conn, metadata, analysis):
    embedded = embedded_recording_id(metadata)
    matches, _, error = fields.recording(conn, analysis, metadata)
    if error and error != 'recording_unconfirmed':
        return None
    ids = {c['recording_id'] for c in matches}
    if embedded:
        ids.add(embedded)
    # An embedded ID cannot overrule a contradictory saved fingerprint.
    for candidate in analysis.get('candidates', []):
        if candidate.get('identification') != 'audio_fingerprint' or (candidate.get('match') or {}).get('state') != 'fingerprint_match':
            continue
        if any(normalized(scalar(metadata.get('tags', {}), k)) != normalized(scalar(candidate.get('tags', {}), k))
               for k in ('title', 'artist')):
            return None
        if candidate.get('provider') == 'acoustid' and candidate.get('recording_id') and ids and candidate['recording_id'] not in ids:
            return None
    return next(iter(ids)) if len(ids) == 1 else None


def performance(doc, identity, metadata):
    if not isinstance(doc, dict) or doc.get('id') != identity:
        return 'identity_unknown'
    query = {k: scalar(metadata['tags'], k) for k in ('title', 'artist')}
    candidate = musicbrainz_candidate(doc, query, metadata['duration'], False)
    if candidate['match']['state'] != 'matching_metadata':
        return 'identity_conflict'
    relations = doc.get('relations') or []
    if any('vocal' in str(rel.get('type', '')).lower() and rel.get('target-type') == 'artist' for rel in relations):
        return 'vocal_credit'
    works = [rel for rel in relations if rel.get('target-type') == 'work' and rel.get('type') == 'performance']
    if not works:
        return 'no_performance_evidence'
    def explicit(rel):
        attributes = rel.get('attributes') or []
        if not rel.get('work', {}).get('id') or 'partial' in attributes or 'medley' in attributes:
            return False
        return ('instrumental' in attributes and
                (rel.get('attribute-ids') or {}).get('instrumental', INSTRUMENTAL) == INSTRUMENTAL)
    return 'instrumental_confirmed' if all(explicit(rel) for rel in works) else 'no_performance_evidence'


def read_input(conn, item):
    row = conn.execute('SELECT * FROM tracks WHERE id=? AND sha256=? AND is_current=1',
                       (item['track_id'], item['sha256'])).fetchone()
    if not row:
        raise media.MediaError('conflict', 'Версия файла изменилась')
    saved = conn.execute('SELECT payload,updated_at FROM analyses WHERE track_id=? AND input_sha256=?',
                         (row['id'], row['sha256'])).fetchone()
    return row, json.loads(row['metadata']), json.loads(saved['payload']) if saved else {}, saved


def inspect_input(settings, item):
    from .edition_artwork import edition_id
    with db.connect(settings.db_path) as conn:
        conn.execute('BEGIN')
        row, meta, analysis, saved = read_input(conn, item)
        group = scope(conn, row, meta, analysis)
        result = {'group': group or 'skipped', 'outcome': 'no_evidence'}
        if not group:
            return result, None, None
        provider_policy.require_track(settings, row, 'derived_analysis', conn=conn)
        if group == 'timing':
            texts = audio_check.candidates(conn, row, analysis, set())
            asr = analysis.get('asr') or {}
            if not texts:
                result['outcome'] = 'reference_unavailable'
            elif not automatic_quality.inference_bound(conn, row, asr) or (audio_check.language(texts) == 'ru' and
                    (asr.get('engine'), asr.get('language')) != ('gigaam', 'ru')):
                result['outcome'] = 'asr_unavailable'
            else:
                detail = audio_check.compare(texts, asr, meta['duration'], detailed=True)
                result.update(outcome=detail.get('classification', detail['state']), timing=detail)
            return result, None, None
        if group == 'cover':
            release = edition_id(conn, row, meta, analysis)
            if 'cover' in field_policy.load(conn, row['id'])['locks']:
                result['outcome'] = 'field_locked'
                return result, None, None
            reference = bound_recording(conn, meta, analysis) if not release and scalar(meta.get('tags',{}),'album') else None
            result['outcome'] = 'edition_unconfirmed'
            return result, release or reference, {'discover':not release,'meta':meta,'revision':saved['updated_at'] if saved else None}
        # An LRCLIB label is an invitation to check the recording, never a decision in itself.
        if meta.get('lyrics') or meta.get('lrc') or (automatic_quality.inference_bound(conn, row, analysis.get('asr') or {})
                and (analysis.get('asr') or {}).get('text', '').strip()):
            result['outcome'] = 'vocal_evidence'
            return result, None, None
        ref = bound_recording(conn, meta, analysis)
        if not ref:
            result['outcome'] = 'identity_unknown'
        return result, ref, meta


async def check_item(worker, job, item):
    def authorize():
        worker.authorize_grant(job)
        if worker.cancelled(job['id']):
            raise media.MediaError('cancelled', 'Проверка остановлена')
    authorize()
    from .worker import root_identity
    if await worker.io(root_identity, worker.settings.work_root) != json.loads(job['payload'])['root']:
        raise media.MediaError('conflict', 'Папка коллекции изменилась')
    result, identity, context = await worker.io(inspect_input, worker.settings, item)
    result['revision'] = item['sha256']
    if not identity:
        return result
    if worker.providers is None:
        worker.providers = Providers(worker.settings, metrics=worker.metrics)
    proof = None
    if result['group'] == 'cover' and context['discover']:
        from .edition_artwork import discover
        if not worker.settings.external_metadata:
            result['outcome'] = 'source_disabled'
            return result
        try:
            provider_policy.check(worker.settings,'musicbrainz','metadata','derived_analysis')
            proof = await discover(worker.providers, identity, context['meta'], authorize)
        except ProviderError as exc:
            result.update(outcome='source_unavailable',code=exc.code)
            return result
        if not proof:
            return result
        identity = proof['release_id']
    if result['group'] == 'cover':
        from .edition_artwork import library_front
        try:
            local = await worker.io(library_front, worker.settings, identity)
        except media.MediaError as exc:
            if exc.code != 'cover_conflict':
                raise
            result['outcome'] = exc.code
            return result
        if local:
            result['outcome'] = await worker.io(save_cover, worker.settings, item, identity, context['revision'], local, authorize, proof)
            result['source'] = 'library'
            return result
    if not worker.settings.external_metadata:
        result['outcome'] = 'source_disabled'
        return result
    try:
        if result['group'] == 'instrumental':
            provider_policy.check(worker.settings, 'musicbrainz', 'metadata', 'derived_analysis')
            url = 'https://' + worker.settings.musicbrainz_host + '/ws/2/recording/' + identity
            doc = await worker.providers.get('musicbrainz', url, {'fmt': 'json', 'inc': 'artist-credits+artist-rels+work-rels'})
            authorize()
            result.update(outcome=performance(doc, identity, context), recording_id=identity)
            if result['outcome'] == 'instrumental_confirmed':
                result['outcome'] = await worker.io(save_instrumental, worker.settings, item, identity, authorize)
        else:
            cover = await worker.providers.cover(identity)
            authorize()
            result['outcome'] = await worker.io(save_cover, worker.settings, item, identity, context['revision'], cover, authorize, proof) if cover else 'no_front'
    except (ProviderError, media.MediaError) as exc:
        if exc.code in {'cancelled', 'conflict', 'authorization_revoked', 'authorization_epoch_changed'}:
            raise
        result.update(outcome='source_unavailable', code=exc.code)
    return result


def save_instrumental(settings, item, identity, authorize):
    authorize()
    with db.transaction(settings.db_path) as conn:
        row, meta, analysis, _ = read_input(conn, item)
        if (scope(conn, row, meta, analysis) != 'instrumental' or meta.get('lyrics') or meta.get('lrc')
                or bound_recording(conn, meta, analysis) != identity
                or (analysis.get('asr') or {}).get('text', '').strip()
                or 'lrc' in field_policy.load(conn, row['id'])['locks']):
            return 'evidence_changed'
        provider_policy.check(settings,'musicbrainz','metadata','cache')
        claims = [c for c in analysis.get('candidates', []) if c.get('provider') == 'lrclib'
                  and (c.get('match') or {}).get('state') == 'matching_metadata']
        if not claims or not all(fields.permitted(conn,c,('cache','derived_analysis')) for c in claims):
            return 'source_restricted'
        meta['instrumental_evidence'] = {'profile':PROFILE,'file_sha256':row['sha256'],'recording_id':identity,
                                        'sources':['lrclib','musicbrainz'],'checked_at':db.utcnow()}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=? AND sha256=? AND is_current=1',
                     (db.encode(meta),row['id'],row['sha256']))
    return 'instrumental_confirmed'


def save_cover(settings, item, identity, revision, cover, authorize, proof=None):
    from .edition_artwork import edition_id
    authorize()
    with db.transaction(settings.db_path) as conn:
        row, meta, analysis, saved = read_input(conn, item)
        if proof:
            attestation = {**proof,'provider':'musicbrainz','kind':'metadata','file_sha256':row['sha256'],
                           'id':'mb-release:'+identity,'source_url':'https://musicbrainz.org/release/'+identity}
            attestation['usage'] = provider_policy.candidate_usage(conn,attestation)
            analysis['verified_catalog_edition'] = attestation
        if (not saved or saved['updated_at'] != revision or edition_id(conn, row, meta, analysis) != identity
                or cover.get('release_id') != identity or meta.get('has_art')):
            return 'edition_changed'
        usage = cover.get('usage') or provider_policy.candidate_usage(conn, cover)
        cover = {**cover, 'usage': usage}
        if not fields.permitted(conn, cover, ('cache', 'embed', 'derived_analysis')):
            return 'source_restricted'
        existing = [c for c in analysis.get('candidates', []) if c.get('provider') == 'coverartarchive'
                    and c.get('release_id') == identity and c.get('cover') and fields.permitted(conn, c)]
        if any(field_policy.normalized_value('cover', c['cover']) != field_policy.normalized_value('cover', cover['cover']) for c in existing):
            return 'cover_conflict'
        if not existing:
            kept = [c for c in analysis['candidates'] if c.get('id') != cover['id']]
            if len(kept) >= 128:
                return 'candidate_limit'
            analysis['candidates'] = [*kept, cover]
            provider_policy.register_values(conn, cover, usage)
        if not existing or proof:
            conn.execute('UPDATE analyses SET payload=?,updated_at=? WHERE track_id=? AND input_sha256=?',
                         (db.encode(analysis), db.utcnow(), row['id'], row['sha256']))
        return 'front_found'

def manifest(conn, scan):
    retained = catalog_sources.completed_sources(conn)
    rows = conn.execute("""SELECT t.id,t.relative_path,t.sha256,t.metadata,t.status
        FROM settings p JOIN json_each(p.value) m JOIN tracks t ON t.id=json_extract(m.value,'$[0]')
        AND t.sha256=json_extract(m.value,'$[1]') AND t.is_current=1
        WHERE p.key GLOB ? AND (NOT json_extract(t.metadata,'$.has_art') OR
        COALESCE(json_extract(t.metadata,'$.lrc'),'')='') ORDER BY t.id""", ('fill_page:' + scan + ':*',))
    for row in rows:
        meta = json.loads(row['metadata'])
        if (row['id'] in retained or row['status'] in {'error','missing','conflict'} or meta.get('input_error')
                or not enrichment_gaps.individual_music(meta, row['sha256'])):
            continue
        need = enrichment_gaps.missing_fields(meta)
        if 'cover' in need or need == ['synced_lyrics']:
            yield {'track_id': row['id'], 'relative_path': row['relative_path'], 'sha256': row['sha256']}


def install(app, settings):
    @app.post('/api/preparation/{run_id}/evidence', status_code=202)
    def start(run_id: uuid.UUID, request: Request):
        from .worker import root_identity
        active = security.require_session(request, settings, True, owner_only=True)
        identity = review_id(run_id)
        with db.transaction(settings.db_path) as conn:
            run = preparation.load(conn)
            if (run.get('id') != str(run_id) or run.get('state') != 'review' or not run.get('autofill')
                    or run.get('epoch') != active['auth_epoch'] or run.get('root') != root_identity(settings.work_root)):
                raise security.error(409, 'preparation_stale', 'Обновите завершённый отчёт папки.')
            if conn.execute('SELECT 1 FROM jobs WHERE id=? AND auth_epoch=?', (identity, run['epoch'])).fetchone():
                return {'job_id': identity}
            if not webhooks.can_accept(conn):
                raise security.error(409, 'preparation_unavailable', 'Дождитесь свободного места в очереди.')
            items = list(manifest(conn, run['scan_id']))
            if not items:
                raise security.error(409, 'no_candidates', 'В этом списке нет записей для проверки.')
            stamp = db.utcnow()
            conn.execute("""INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch)
                VALUES(?,?,'queued','pending',?,?,?,?,?)""", (identity,KIND,db.encode({'items':items,'run_id':run['id'],
                'root':run['root'],'params':{}}),len(items),stamp,stamp,run['epoch']))
            db.event(conn,'job_created',{'job_id':identity,'kind':KIND})
        return {'job_id': identity}

    @app.get('/api/preparation/{run_id}/evidence')
    def report(run_id: uuid.UUID, request: Request, group: str = Query('timing', pattern='^(timing|cover|instrumental)$'),
               offset: int = Query(0,ge=0)):
        active = security.require_session(request, settings, owner_only=True)
        identity = review_id(run_id)
        with db.connect(settings.db_path) as conn:
            conn.execute('BEGIN')
            job = conn.execute('SELECT state,total,completed,failed FROM jobs WHERE id=? AND auth_epoch=? AND kind=?',
                               (identity,active['auth_epoch'],KIND)).fetchone()
            if not job:
                return {'state':'idle', 'items':[], 'counts':{}}
            counts = {}
            for r in conn.execute("""SELECT json_extract(result,'$.group') AS g,json_extract(result,'$.outcome') AS o,
                    COUNT(*) AS n FROM job_items WHERE job_id=? AND state='succeeded' GROUP BY g,o""",(identity,)):
                if r['g'] and r['g'] != 'skipped':
                    counts.setdefault(r['g'],{})[r['o']] = r['n']
            items = []
            for r in conn.execute("""SELECT i.item_key,i.result,t.relative_path FROM job_items i LEFT JOIN tracks t ON t.id=i.item_key
                    WHERE i.job_id=? AND i.state='succeeded' AND json_extract(i.result,'$.group')=?
                    ORDER BY t.relative_path,i.item_key LIMIT 20 OFFSET ?""",(identity,group,offset)):
                items.append({'track_id':r['item_key'],'file':r['relative_path'], **json.loads(r['result'])})
            return {**dict(job),'job_id':identity,'counts':counts,'items':items}

    @app.get('/api/preparation/{run_id}/evidence/covers')
    def applicable(run_id: uuid.UUID, request: Request):
        active = security.require_session(request, settings, owner_only=True)
        identity = review_id(run_id)
        with db.connect(settings.db_path) as conn:
            conn.execute('BEGIN')
            job = conn.execute('SELECT state FROM jobs WHERE id=? AND auth_epoch=? AND kind=?',
                               (identity,active['auth_epoch'],KIND)).fetchone()
            if not job or job['state'] not in ('succeeded','completed_with_errors'):
                raise security.error(409,'preparation_stale','Дождитесь завершения проверки.')
            rows = conn.execute("""SELECT t.* FROM job_items i JOIN tracks t ON t.id=i.item_key AND t.is_current=1
                    AND t.sha256=json_extract(i.result,'$.revision') WHERE i.job_id=? AND i.state='succeeded'
                    AND json_extract(i.result,'$.outcome')='front_found' ORDER BY t.id""",(identity,))
            busy, retired = fields.active_tracks(conn), catalog_sources.completed_sources(conn)
            tokens = {}
            for row in rows:
                plan = fields.evaluate(settings, conn, row, active, busy=busy, retired=retired)
                if plan['ready'] and [v['field'] for v in plan['changes']] == ['cover']:
                    tokens[row['id']] = plan['token']
                    if len(tokens) == 200:
                        break
            return {'tokens':tokens}
