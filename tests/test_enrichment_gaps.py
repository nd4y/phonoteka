import json
import time
import tracemalloc
import uuid

import pytest

from phonoteka import db, enrichment_gaps
from phonoteka.providers import album_candidates
from test_backend import authenticated, client, seed, settings


def analysis(settings, track_id, sha, payload):
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',
                     (track_id, sha, db.encode(payload), db.utcnow()))


def gaps(client, identity):
    response = client.get('/api/tracks/' + identity)
    assert response.status_code == 200, response.text
    return {r['field']: r for r in response.json()['enrichment_gaps']}


def failed_search(settings, identity, sha, *, kind='analyze', created='2090-01-02T00:00:00+00:00'):
    job_id = str(uuid.uuid4())
    with db.transaction(settings.db_path) as conn:
        conn.execute('''INSERT INTO jobs(id,kind,state,stage,payload,created_at,updated_at)
            VALUES(?,?,'completed_with_errors','finished',?,?,?)''',
            (job_id, kind, db.encode({'items': [{'track_id': identity, 'sha256': sha}],
                                      'private': 'private-job-payload'}), created, '2090-01-04T00:00:00+00:00'))
        conn.execute('INSERT INTO job_items VALUES(?,?,?,?)',
                     (job_id, identity, 'failed', db.encode({'error': 'private-error'})))
    return job_id


@pytest.mark.parametrize('kind,action', [('analyze', 'search'), ('recognize', 'sources')])
def test_failed_search_supersedes_old_flags_without_losing_evidence(client, settings, kind, action):
    authenticated(client, settings)
    identity, sha = seed(settings)
    payload = {'sources': [{'provider': 'lrclib', 'state': 'no_match'}]}
    analysis(settings, identity, sha, payload)
    job_id = failed_search(settings, identity, sha, kind=kind)
    values = gaps(client, identity)
    assert values['cover']['code'] == 'search_failed'
    assert values['synced_lyrics']['code'] == 'search_failed'
    assert values['cover']['action'] == action
    assert 'Прежние результаты поиска сохранены.' in values['cover']['next_step']
    assert values['cover']['failed_job_id'] == job_id
    assert values['cover']['checked_at'] == '2090-01-04T00:00:00+00:00'
    page = client.get('/api/tracks').json()['items'][0]
    assert next(g for g in page['enrichment_gaps'] if g['field'] == 'cover')['code'] == 'search_failed'
    with db.connect(settings.db_path) as conn:
        projected = enrichment_gaps.project(conn, [identity])
        saved = conn.execute('SELECT payload FROM analyses WHERE track_id=?', (identity,)).fetchone()
    assert json.loads(saved['payload']) == payload
    assert 'private-' not in json.dumps(projected)
    assert 'private-' not in json.dumps(page)


def test_first_failed_search_and_field_specific_saved_proposal(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    failed_search(settings, identity, sha)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',
                     ('p', identity, sha, db.encode({'tags': {'album': 'Album'}}), db.utcnow()))
    values = gaps(client, identity)
    assert values['album']['code'] == 'proposal_ready'
    assert values['cover']['code'] == 'search_failed'
    assert values['synced_lyrics']['code'] == 'search_failed'
    assert 'Прежние результаты' not in values['cover']['next_step']


@pytest.mark.parametrize('case', ['changed_file', 'unrelated_job', 'replaced_analysis'])
def test_unrelated_or_superseded_failure_does_not_override_current_analysis(client, settings, case):
    authenticated(client, settings)
    identity, sha = seed(settings)
    analysis(settings, identity, sha, {'sources': [{'provider': 'lrclib', 'state': 'no_match'}]})
    failed_search(settings, identity, 'b'*64 if case == 'changed_file' else sha,
                  kind='apply' if case == 'unrelated_job' else 'analyze')
    if case == 'replaced_analysis':
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE analyses SET updated_at=? WHERE track_id=?',
                         ('2090-01-03T00:00:00+00:00', identity))
    values = gaps(client, identity)
    assert values['cover']['code'] == 'cover_album_unconfirmed'
    assert values['synced_lyrics']['code'] == 'no_result_for_query'


def test_album_release_is_visible_and_selectable_without_changing_title(client, settings):
    headers=authenticated(client,settings);identity,sha=seed(settings)
    candidates=album_candidates([{'releases':[{'id':'00000000-0000-0000-0000-000000000001','title':'Album','date':'2001'}]}],'')
    analysis(settings,identity,sha,{'sources':[{'provider':'musicbrainz','state':'candidates'}],'candidates':candidates})
    value=gaps(client,identity)['album']
    assert value['code']=='candidates_require_review' and value['candidate_count']==1
    response=client.post('/api/tracks/'+identity+'/analysis/select',headers={**headers,'If-Match':sha},json={'candidate_ids':[candidates[0]['id']]})
    assert response.status_code==201,response.text
    payload=json.loads(db.fetchone(settings.db_path,'SELECT payload FROM proposals WHERE id=?',(response.json()['id'],))['payload'])
    assert payload['tags']['album']=='Album' and 'title' not in payload['tags'] and 'artist' not in payload['tags']
    assert payload['field_sources']['album']['provider']=='musicbrainz'
    assert client.get('/api/tracks/'+identity).json()['tags']['title']=='Example'


def test_reasons_are_current_and_field_specific(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    assert gaps(client, identity)['cover']['code'] == 'not_searched'
    analysis(settings, identity, sha, {'sources': [{'provider':'lrclib','state':'no_match'}]})
    current = gaps(client, identity)
    assert current['cover']['code'] == 'cover_album_unconfirmed'
    assert current['synced_lyrics']['code'] == 'no_result_for_query'
    assert 'невозможно' not in current['synced_lyrics']['message']
    analysis(settings, identity, 'b'*64, {'sources': [{'provider':'lrclib','state':'no_match'}]})
    assert gaps(client, identity)['synced_lyrics']['code'] == 'search_stale'


def test_proposal_only_explains_fields_it_will_fill(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',
                     ('p', identity, sha, db.encode({'tags': {'album': 'Album'}, 'lrc': 'Plain text'}), db.utcnow()))
    values = gaps(client, identity)
    assert values['album']['code'] == 'proposal_ready'
    assert values['cover']['code'] == 'not_searched'
    assert values['synced_lyrics']['code'] != 'proposal_ready'


def test_timed_lyrics_draft_opens_audio_review_and_ignores_stale_coverage(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',
                     ('p', identity, sha, db.encode({'lrc': '[00:01]First line\n[00:03]Second line'}), db.utcnow()))
    analysis(settings, identity, sha, {'alignment': {'coverage': 0.25, 'complete': False}})
    draft = gaps(client, identity)['synced_lyrics']
    assert (draft['code'], draft['action']) == ('lyrics_draft_needs_review', 'lyrics')
    assert draft['last_alignment_coverage'] == 0.25

    analysis(settings, identity, sha, {'alignment': {'coverage': 0.25, 'stale': True}})
    assert 'last_alignment_coverage' not in gaps(client, identity)['synced_lyrics']
    analysis(settings, identity, 'b'*64, {'alignment': {'coverage': 0.25}})
    assert 'last_alignment_coverage' not in gaps(client, identity)['synced_lyrics']


def test_retry_does_not_claim_not_found_or_pollute_other_fields(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    analysis(settings, identity, sha, {'sources': [{'provider':'lrclib','state':'rate_limited'}]})
    values = gaps(client, identity)
    assert values['synced_lyrics']['code'] == 'provider_retry'
    assert values['synced_lyrics']['provider'] == 'lrclib'
    assert values['cover']['code'] == 'cover_album_unconfirmed'


@pytest.mark.parametrize('case,code,action', [
    ('missing_artist', 'cover_recording_unconfirmed', 'sources'),
    ('known_album', 'cover_edition_unconfirmed', 'sources'),
    ('ambiguous_release', 'cover_edition_unconfirmed', 'sources'),
    ('different_album', 'cover_edition_unconfirmed', 'sources'),
    ('single_release', 'cover_not_searched', 'search'),
    ('pinned_release', 'cover_not_searched', 'search'),
])
def test_artwork_explains_the_missing_recording_or_edition(client, settings, case, code, action):
    authenticated(client, settings)
    identity, sha = seed(settings)
    release = '00000000-0000-0000-0000-000000000001'
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone()
        meta = json.loads(row['metadata'])
        meta['tags']['album'] = 'Альбом'
        if case == 'missing_artist':
            del meta['tags']['artist']
        if case == 'pinned_release':
            meta['tags']['musicbrainz_albumid'] = release
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), identity))
    candidates = []
    if case in {'single_release', 'different_album', 'ambiguous_release'}:
        releases = [{'id': release, 'title': 'Другое издание' if case == 'different_album' else 'альбом'}]
        if case == 'ambiguous_release':
            releases.append({'id': '00000000-0000-0000-0000-000000000002', 'title': 'Альбом'})
        candidates = album_candidates([{'releases': releases}], '')
    analysis(settings, identity, sha, {'sources': [{'provider':'musicbrainz','state':'candidates'}],
                                       'candidates': candidates})
    cover = gaps(client, identity)['cover']
    assert (cover['code'], cover['action']) == (code, action)
    assert cover['message'] and cover['next_step']


def test_instrumental_claim_never_means_lyrics_not_applicable(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    analysis(settings, identity, sha, {'sources': [{'provider':'lrclib','state':'candidates'}],
        'candidates':[{'kind':'lyrics','provider':'local','instrumental_claim':True}]})
    value = gaps(client, identity)['synced_lyrics']
    assert value['code'] == 'instrumental_unconfirmed'
    assert value['action'] == 'sources'


def test_connected_acoustid_supersedes_historical_missing_key(client, settings, monkeypatch):
    authenticated(client, settings)
    monkeypatch.setattr(enrichment_gaps.acoustid, 'public_connection', lambda _: {'configured':True})
    identity, sha = seed(settings)
    analysis(settings, identity, sha, {'sources': [{'provider':'acoustid','state':'key_required'}]})
    assert gaps(client, identity)['album']['code'] == 'connection_now_ready'


def test_exhausted_recognition_is_not_a_temporary_provider_pause(client,settings):
    authenticated(client,settings);identity,sha=seed(settings)
    analysis(settings,identity,sha,{'sources':[{'provider':'shazam','state':'recognition_attempt_budget_exhausted'}]})
    value=gaps(client,identity)['album']
    assert value['code']=='recognition_limit' and value['action']=='sources'
    assert 'не сбрасывает' in value['next_step']
    analysis(settings,identity,sha,{'sources':[{'provider':'shazam','state':'unknown_outcome'}]})
    assert gaps(client,identity)['album']['code']=='provider_outcome_unknown'


def test_new_connection_does_not_suggest_retrying_spent_audio_identity(client,settings,monkeypatch):
    authenticated(client,settings);identity,sha=seed(settings)
    monkeypatch.setattr(enrichment_gaps.acoustid,'public_connection',lambda _: {'configured':True})
    with db.transaction(settings.db_path) as conn:
        row=conn.execute('SELECT metadata FROM tracks WHERE id=?',(identity,)).fetchone()
        meta=json.loads(row['metadata']);meta['audit']={'audio_identity':'b'*64}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),identity))
        conn.execute('INSERT INTO settings VALUES(?,?)',('recognition_usage:'+'b'*64,db.encode({'calls':3})))
    analysis(settings,identity,sha,{'sources':[{'provider':'acoustid','state':'key_required'}]})
    assert gaps(client,identity)['album']['code']=='recognition_limit'


def test_cover_waits_for_release_search_and_policy_denial_is_explicit(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    analysis(settings, identity, sha, {'sources': [{'provider':'musicbrainz','state':'provider_cooldown'},
        {'provider':'lrclib','state':'provider_policy_denied'}]})
    current = gaps(client, identity)
    assert current['cover']['code'] == 'provider_retry'
    assert current['cover']['provider'] == 'musicbrainz'
    assert current['synced_lyrics']['code'] == 'source_policy_restricted'


def test_candidate_expiry_visible_without_returning_lyrics(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO settings VALUES(?,?)', ('provider_usage:lrclib:lyrics', db.encode({
            'allow_fetch':True,'allow_cache':True,'allow_embed':True,'cache_ttl':60})))
    analysis(settings, identity, sha, {'candidates':[{'kind':'lyrics','provider':'lrclib',
        'lrc':'[00:01]Expired', 'usage': {'origin':'provider_acquired','provider':'lrclib',
        'data_type':'lyrics','allow_cache':True,'allow_embed':True,'cache_ttl':60,'acquired_at':time.time()-1000}}]})
    assert gaps(client, identity)['synced_lyrics']['code'] == 'results_expired'


def test_partial_asr_evidence_is_not_reported_as_complete_lyrics(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    analysis(settings, identity, sha, {'alignment':{'coverage':0.25,'complete':False,'lines':[]},
        'sources':[{'provider':'lrclib','state':'no_match'}]})
    values = gaps(client, identity)
    assert values['synced_lyrics']['last_alignment_coverage']==0.25
    assert values['synced_lyrics']['code']=='no_result_for_query'
    assert 'last_alignment_coverage' not in values['cover']


def test_catalog_projection_does_not_load_cover_or_transcript_payloads(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    private='private-payload-'+'x'*(4*1024*1024)
    analysis(settings, identity, sha, {'asr':{'text':private},'candidates':[
        {'kind':'cover','provider':'local_cover','cover':private},
        {'kind':'lyrics','provider':'local','lrc':private}]})
    tracemalloc.start()
    try:
        with db.connect(settings.db_path) as conn:
            projected=enrichment_gaps.project(conn,[identity])
        _,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak<1024*1024
    assert 'private-payload-' not in json.dumps(projected)
    page=client.get('/api/tracks').json()['items'][0]
    assert 'private-payload-' not in json.dumps(page)
    assert next(g for g in page['enrichment_gaps'] if g['field']=='cover')['candidate_count']==1
