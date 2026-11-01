import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
from urllib.parse import parse_qs

import httpx
import pytest

from phonoteka import acoustid, db, media, provider_attempts, reports
from phonoteka.providers import Providers, ProviderError
from test_backend import authenticated, client, settings, seed
from test_providers import settings as provider_settings

CLIENT = 'fixturekey123'
SIGNATURE = 'AQAAA_fixture_signature'
TRACK = '9ff43b6a-4f16-427c-93c2-92307ca505e0'
RECORDING = 'cd2e7c47-16f5-46c6-a37c-a1eb7bf599ff'


def result():
    return {'status': 'ok', 'results': [{'id': TRACK, 'score': .99,
        'recordings': [{'id': RECORDING, 'title': 'Known song', 'artists': [{'name': 'Known artist'}]}]}]}


def configured(tmp_path):
    cfg = replace(provider_settings(tmp_path), external_fingerprints=True)
    acoustid.save_connection(cfg, 'initial', True, CLIENT)
    return cfg


def test_owner_connection_secrecy_conflict_delete_and_csrf(client, settings):
    url = '/api/integrations/acoustid'
    assert client.get(url).status_code == 401
    headers = authenticated(client, settings)
    value = client.get(url).json()
    assert value['key_set'] is False
    body = {'version': value['version'], 'enabled': True, 'api_key': CLIENT}
    assert client.put(url, json=body).status_code == 403
    assert client.put(url, json=body, headers={'Authorization': 'Bearer arbitrary-token'}).status_code == 403
    saved = client.put(url, json=body, headers=headers)
    assert saved.status_code == 200 and saved.json()['key_set'] is True
    assert saved.json()['configured'] is False  # server transmission consent remains authoritative
    assert CLIENT not in saved.text + client.get(url).text + client.get('/api/providers').text
    path = settings.state_dir/'integrations/acoustid.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert CLIENT in path.read_text()
    assert CLIENT not in str(db.fetchall(settings.db_path, 'SELECT * FROM settings'))
    assert client.put(url, json=body, headers=headers).status_code == 409
    new = saved.json()
    assert client.put(url, json={'version': new['version'], 'enabled': False, 'api_key': 'invalid key '+CLIENT}, headers=headers).status_code == 422
    current = client.get(url).json()
    assert current['version'] == new['version']
    replacement = 'replacementfixture9'
    replaced = client.put(url, json={'version': current['version'], 'enabled': True, 'api_key': replacement}, headers=headers)
    assert replaced.status_code == 200 and replacement not in replaced.text
    assert CLIENT not in path.read_text() and replacement in path.read_text()
    current = replaced.json()
    deleted = client.put(url, json={'version': current['version'], 'enabled': False, 'api_key': ''}, headers=headers)
    assert deleted.status_code == 200 and not deleted.json()['key_set']
    assert CLIENT not in path.read_text() and replacement not in path.read_text()


def test_unsafe_connection_file_is_rejected(tmp_path):
    cfg = configured(tmp_path)
    path = cfg.state_dir/'integrations/acoustid.json'
    path.chmod(0o644)
    with pytest.raises(media.MediaError, match='Небезопасный'): acoustid.connection(cfg)
    path.unlink(); target = tmp_path/'elsewhere'; target.write_text('{}'); path.symlink_to(target)
    with pytest.raises(OSError): acoustid.connection(cfg)
    assert target.read_text() == '{}'


def test_real_chromaprint_is_bounded_local_and_does_not_modify_audio(tmp_path):
    cfg = configured(tmp_path)
    for root in cfg.roots.values(): root.mkdir()
    path = cfg.work_root/'private song.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=15', str(path)], check=True)
    before = media.sha256_file(path)
    value = acoustid.fingerprint(path, cfg.roots)
    assert 10 <= len(value) <= 20000 and 'private' not in value
    assert before == media.sha256_file(path)
    assert acoustid.probe_signature(cfg)


def test_lookup_posts_only_fingerprint_key_duration_caches_and_spends_shared_budget(tmp_path):
    cfg = configured(tmp_path); calls = []
    def respond(request):
        calls.append(request)
        assert str(request.url) == acoustid.ENDPOINT and request.method == 'POST'
        payload = parse_qs(request.content.decode())
        assert set(payload) == {'client', 'fingerprint', 'duration', 'meta', 'format'}
        assert payload['client'] == [CLIENT] and payload['duration'] == ['240']
        return httpx.Response(200, json=result())
    async def run():
        providers = Providers(cfg, httpx.MockTransport(respond))
        with provider_attempts.recognition_scope('a'*64, 'test'), provider_attempts.input_revision('b'*64):
            first = await acoustid.identify(providers, SIGNATURE, 240)
            second = await acoustid.identify(providers, SIGNATURE, 240)
        assert first == second and len(calls) == 1
        assert first[0]['tags']['musicbrainz_trackid'] == RECORDING
        assert first[0]['recording_version_verified'] is False
        assert provider_attempts.report(cfg)['recognition_budget']['spent_calls'] == 1
        for table in ('settings', 'provider_cache'):
            assert CLIENT not in str(db.fetchall(cfg.db_path, 'SELECT * FROM '+table))
        await providers.close()
    asyncio.run(run())


@pytest.mark.parametrize('response,code', [
    (httpx.Response(200, json={'status':'error','error':{'code':4,'message':CLIENT}}), 'provider_unauthorized'),
    (httpx.Response(400, json={'status':'error','error':{'code':4,'message':CLIENT}}), 'provider_unauthorized'),
    (httpx.Response(429), 'rate_limited'),
    (httpx.Response(302, headers={'location':'https://evil.example/'+CLIENT}), 'provider_unavailable'),
    (httpx.Response(200, json={'status':'ok','results':[{'id':TRACK,'score':'bad'}]}), 'provider_unavailable'),
    (httpx.Response(200, content=b'x'*(2*1024**2+1)), 'provider_unavailable'),
])
def test_errors_never_expose_key_or_retry(tmp_path, response, code):
    cfg = configured(tmp_path); calls = []
    async def run():
        providers = Providers(cfg, httpx.MockTransport(lambda req: (calls.append(req) or response)))
        with provider_attempts.recognition_scope('a'*64):
            with pytest.raises(ProviderError) as error: await acoustid.identify(providers, SIGNATURE, 240)
        assert error.value.code == code and CLIENT not in str(error.value)
        assert len(calls) == 1
        assert not db.fetchall(cfg.db_path, 'SELECT * FROM provider_cache')
        await providers.close()
    asyncio.run(run())


def test_unknown_network_outcome_is_spent_once(tmp_path):
    cfg = configured(tmp_path); calls = []
    def fail(request):
        calls.append(request); raise httpx.ReadTimeout('private diagnostic '+CLIENT)
    async def run():
        providers = Providers(cfg, httpx.MockTransport(fail))
        with provider_attempts.recognition_scope('a'*64):
            with pytest.raises(ProviderError) as error: await acoustid.identify(providers, SIGNATURE, 240)
        assert error.value.code == 'unknown_outcome' and CLIENT not in str(error.value)
        assert len(calls) == 1 and provider_attempts.report(cfg)['items'][0]['state'] == 'unknown_outcome'
        await providers.close()
    asyncio.run(run())


def test_cancelled_disabled_and_exhausted_lookup_do_not_send(tmp_path):
    cfg = configured(tmp_path); calls = []
    def cancel(): raise media.MediaError('authorization_revoked','cancelled')
    async def run():
        providers = Providers(cfg, httpx.MockTransport(lambda req:(calls.append(req) or httpx.Response(200,json=result()))))
        with pytest.raises(media.MediaError): await acoustid.identify(providers, SIGNATURE, 240, cancel)
        with provider_attempts.recognition_scope('a'*64):
            for _ in range(3): provider_attempts.reserve(cfg, 'shazam', 'fingerprint')
            with pytest.raises(ProviderError) as error: await acoustid.identify(providers, SIGNATURE, 240)
            assert error.value.code == 'recognition_attempt_budget_exhausted'
        current = acoustid.connection(cfg)
        acoustid.save_connection(cfg,current['version'],False)
        with pytest.raises(ProviderError) as error: await acoustid.identify(providers, SIGNATURE,240)
        assert error.value.code == 'provider_disabled' and calls == []
        await providers.close()
    asyncio.run(run())


def test_report_counts_fallback_match_once(client, settings):
    identity, sha = seed(settings)
    payload={'state':'candidates','candidates':acoustid.candidates(result()['results'],240),
        'sources':[{'provider':'shazam','state':'no_match'},{'provider':'acoustid','state':'fingerprint_match'}]}
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',(identity,sha,db.encode(payload),db.utcnow()))
    counts=reports.summary(settings)['counts']
    assert counts['fingerprint_matches'] == 1 and counts['fingerprint_no_match'] == 0 and counts['recognition_pending'] == 0
