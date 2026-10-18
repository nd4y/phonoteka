import asyncio
from dataclasses import replace
import json
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from phonoteka import db, media, provider_policy as policy
from phonoteka.app import create_app
from phonoteka.providers import Providers, ProviderError
from test_backend import authenticated, client, settings, seed


def grant(conn, provider='lrclib', kind='lyrics', **changes):
    value = {**policy.defaults(provider, kind), **{'allow_' + name: True for name in policy.OPERATIONS},
             'cache_ttl': 3600, 'basis': 'Owner fixture permission for explicit operations', **changes}
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (f'provider_usage:{provider}:{kind}', db.encode(value)))
    return policy.rule(conn, provider, kind)


def lyrics():
    return {'id': 'lrclib:123', 'provider': 'lrclib', 'kind': 'lyrics', 'lrc': '[00:01]Known provider words',
            'source_url': 'https://lrclib.net/api/get/123', 'match': {'state': 'needs_review', 'reasons': []}}


def test_unknown_capabilities_fail_closed_and_core_metadata_does_not_claim_content_license(client, settings):
    with db.connect(settings.db_path) as conn:
        assert all(policy.rule(conn, 'musicbrainz', 'metadata')['allow_' + operation] for operation in policy.OPERATIONS)
        assert not policy.rule(conn, 'musicbrainz', 'cover')['allow_embed']
        for provider, kind in [('lrclib', 'lyrics'), ('coverartarchive', 'cover'), ('shazam', 'metadata')]:
            rule = policy.rule(conn, provider, kind)
            assert rule['allow_fetch'] and not rule['allow_cache'] and not rule['allow_embed'] and not rule['allow_export'] and not rule['allow_derived_analysis']
        assert not policy.rule(conn, 'unknown-service', 'metadata')['allow_fetch']


def test_transient_provider_response_is_not_cached_and_fetch_denial_precedes_network(client, settings):
    calls = []
    async def run():
        providers = Providers(settings, httpx.MockTransport(lambda request: (calls.append(str(request.url)) or httpx.Response(200, json={'syncedLyrics': 'private words'}))))
        async def throttle(_provider):
            return None
        providers.throttle = throttle
        assert await providers.get('lrclib', 'https://lrclib.net/api/get/123')
        assert await providers.get('lrclib', 'https://lrclib.net/api/get/123')
        assert len(calls) == 2 and db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM provider_cache')['n'] == 0
        with db.transaction(settings.db_path) as conn:
            grant(conn, allow_fetch=False)
        with pytest.raises(ProviderError) as denied:
            await providers.get('lrclib', 'https://lrclib.net/api/get/123')
        assert denied.value.code == 'provider_policy_denied' and len(calls) == 2
        await providers.close()
    asyncio.run(run())


def test_manual_copy_and_time_edits_do_not_erase_acquired_restrictions(client, settings):
    track_id, _ = seed(settings)
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
    with db.transaction(settings.db_path) as conn:
        data = policy.prepare_analysis(conn, {'candidates': [lyrics()]})
        candidate = data['candidates'][0]
        assert candidate['transient_only'] and 'lrc' not in candidate
        assert candidate['usage']['license_verified'] is False
        with pytest.raises(media.MediaError):
            policy.require_payload(conn, row, {'lrc': '[00:05]Known provider words'}, 'cache')
        grant(conn)
        # A new policy is not a retroactive license or a manual-copy escape.
        with pytest.raises(media.MediaError):
            policy.require_payload(conn, row, {'lrc': '[00:06]Known provider words'}, 'embed')
        new = lyrics(); new['usage'] = policy.candidate_usage(conn, new)
        approved = policy.require_payload(conn, row, {'lrc': new['lrc'], 'field_sources': {'lrc': {'usage': new['usage']}}}, 'embed')
        assert approved['field_sources']['lrc']['usage']['policy_version'] == new['usage']['policy_version']


def test_preexisting_and_owner_supplied_fields_keep_manual_flow_without_license_claim(client, settings):
    track_id, _ = seed(settings, lrc='[00:01]Existing personal text')
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
    with db.connect(settings.db_path) as conn:
        preexisting = policy.require_payload(conn, row, {'tags': {'title': 'Example'}, 'lrc': '[00:01]Existing personal text'}, 'embed')
        assert preexisting['field_sources']['title']['origin'] == 'preexisting'
        assert preexisting['field_sources']['lrc']['origin'] == 'preexisting'
        supplied = policy.require_payload(conn, row, {'tags': {'title': 'Owner supplied title'}}, 'embed')
        assert supplied['field_sources']['title']['origin'] == 'owner_supplied'
        assert supplied['field_sources']['title']['license_verified'] is False


def test_policy_change_blocks_export_and_backup_without_deleting_music(client, settings):
    track_id, _ = seed(settings)
    with db.transaction(settings.db_path) as conn:
        grant(conn)
        source = {'usage': policy.candidate_usage(conn, lyrics())}
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (track_id,)).fetchone()
        meta = json.loads(row['metadata']); meta['usage_provenance'] = {'lrc': source}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track_id))
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
    entry = {'usage_provenance': {'lrc': source}}
    policy.require_track(settings, row, 'export'); policy.require_export_entry(settings, entry)
    before = (settings.work_root / 'a.mp3').read_bytes()
    with db.transaction(settings.db_path) as conn:
        grant(conn, allow_export=False, allow_cache=False)
    with pytest.raises(media.MediaError): policy.require_export_entry(settings, entry)
    with pytest.raises(media.MediaError): policy.require_backup(settings)
    assert (settings.work_root / 'a.mp3').read_bytes() == before
    report = policy.provenance_report(settings)
    assert report['files'][0]['findings'][0]['denied_operations']
    assert not report['music_deleted'] and not report['rollback_versions_deleted']


def test_expired_cache_is_cleaned_in_analysis_proposal_and_queued_job(client, settings):
    track_id, sha = seed(settings)
    with db.transaction(settings.db_path) as conn:
        grant(conn, cache_ttl=1)
        candidate = lyrics(); usage = policy.candidate_usage(conn, candidate, now=time.time() - 5)
        candidate['usage'] = usage
        payload = {'lrc': candidate['lrc'], 'field_sources': {'lrc': {'usage': usage}}}
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track_id, sha, db.encode({'candidates': [candidate]}), db.utcnow()))
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('proposal', track_id, sha, db.encode(payload), db.utcnow()))
        conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at) VALUES('job','apply','queued','pending',?,1,?,?)", (db.encode({'items': [{'track_id': track_id, 'proposal': payload}]}), db.utcnow(), db.utcnow()))
    result = policy.cleanup_cache(settings)
    assert result['analyses_cleaned'] == 1
    assert 'Known provider words' not in db.fetchone(settings.db_path, 'SELECT payload FROM analyses')['payload']
    assert 'Known provider words' not in db.fetchone(settings.db_path, 'SELECT payload FROM proposals')['payload']
    task = db.fetchone(settings.db_path, "SELECT * FROM jobs WHERE id='job'")
    assert task['state'] == 'cancelled' and task['error'] == 'provider_policy_expired'
    assert 'Known provider words' not in task['payload']


def test_inherited_alignment_constraints_cannot_be_laundered_as_local_asr(client, settings):
    with db.connect(settings.db_path) as conn:
        parent = {**policy.defaults('lrclib', 'lyrics'), 'origin': 'provider_acquired'}
        child = {**policy.defaults('independent_asr', 'lyrics'), 'origin': 'owner_supplied', 'inherited_restrictions': [parent]}
        with pytest.raises(media.MediaError): policy.require_sources(conn, [child], 'export')


def test_policy_owner_api_has_csrf_revision_and_no_implicit_grants(client, settings):
    assert client.get('/api/provider-policy').status_code == 401
    headers = authenticated(client, settings)
    current = next(item for item in client.get('/api/provider-policy').json()['providers'] if item['id'] == 'lrclib')['rules']
    initial = next(rule for rule in current if rule['data_type'] == 'lyrics')
    body = {key: initial[key] for key in ('data_type', 'allow_fetch', 'allow_cache', 'allow_embed', 'allow_export', 'allow_derived_analysis', 'cache_ttl', 'terms_url', 'attribution')}
    body.update(basis='Owner reviewed fixture permission', base_version=initial['policy_version'])
    assert client.put('/api/provider-policy/lrclib', json=body).status_code == 403
    response = client.put('/api/provider-policy/lrclib', headers=headers, json=body)
    assert response.status_code == 200
    assert not response.json()['rule']['allow_embed'] and not response.json()['rule']['allow_export']
    assert response.json()['rule']['license_verified'] is False
    assert client.put('/api/provider-policy/lrclib', headers=headers, json=body).status_code == 412
    assert client.get('/api/reports/provenance').status_code == 200


def test_transient_preview_stays_in_response_without_persisting_lyrics(settings, monkeypatch):
    cfg = replace(settings, external_metadata=True)
    async def get(_self, provider, url, *args, **kwargs):
        assert provider == 'lrclib' and url == 'https://lrclib.net/api/get/123'
        return {'syncedLyrics': '[00:01]Transient private words'}
    monkeypatch.setattr(Providers, 'get', get)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as instance:
        headers = authenticated(instance, cfg); track_id, sha = seed(cfg)
        with db.transaction(cfg.db_path) as conn:
            data = policy.prepare_analysis(conn, {'candidates': [lyrics()]})
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track_id, sha, db.encode(data), db.utcnow()))
        response = instance.post(f'/api/tracks/{track_id}/provider-preview', headers=headers, json={'candidate_id': 'lrclib:123'})
        assert response.status_code == 200 and response.json()['transient_only']
        assert response.headers['cache-control'] == 'private, no-store'
        assert 'Transient private words' not in db.fetchone(cfg.db_path, 'SELECT payload FROM analyses')['payload']
        assert db.fetchone(cfg.db_path, 'SELECT COUNT(*) AS n FROM provider_cache')['n'] == 0
