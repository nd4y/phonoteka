import json
import time
import tracemalloc

from phonoteka import db, enrichment_gaps
from test_backend import authenticated, client, seed, settings


def analysis(settings, track_id, sha, payload):
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',
                     (track_id, sha, db.encode(payload), db.utcnow()))


def gaps(client, identity):
    response = client.get('/api/tracks/' + identity)
    assert response.status_code == 200, response.text
    return {r['field']: r for r in response.json()['enrichment_gaps']}


def test_reasons_are_current_and_field_specific(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    assert gaps(client, identity)['cover']['code'] == 'not_searched'
    analysis(settings, identity, sha, {'sources': [{'provider':'lrclib','state':'no_match'}]})
    current = gaps(client, identity)
    assert current['cover']['code'] == 'cover_not_searched'
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


def test_retry_does_not_claim_not_found_or_pollute_other_fields(client, settings):
    authenticated(client, settings)
    identity, sha = seed(settings)
    analysis(settings, identity, sha, {'sources': [{'provider':'lrclib','state':'rate_limited'}]})
    values = gaps(client, identity)
    assert values['synced_lyrics']['code'] == 'provider_retry'
    assert values['synced_lyrics']['provider'] == 'lrclib'
    assert values['cover']['code'] == 'cover_not_searched'


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
