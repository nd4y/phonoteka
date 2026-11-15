"""Real worker/provider transport for a bounded lyric-only preparation."""
import asyncio
import copy
import json
import time
import uuid

import httpx
import pytest

from phonoteka import catalogs, db, media, metadata_preparation as prep, provider_policy, worker
from phonoteka.providers import Providers
from test_backend import drain
from test_lyrics_preparation import library, lyric_only, settings


def start(client, headers):
    item, = client.get('/api/confirmed-fields/plan').json()['items']
    assert item['preparation']['lyric_search_only'] is True
    response = client.post('/api/metadata-preparation',
        json={'tokens': {item['track_id']: item['preparation']['token']}},
        headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize('status', [200, 404, 429, 503])
def test_native_worker_queries_only_lrclib_and_keeps_other_evidence(library, monkeypatch, status):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    track = client.get('/api/tracks/' + tid).json()
    with db.transaction(cfg.db_path) as conn:
        old = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()[0])
        preserved = {'id': 'mb:kept', 'kind': 'metadata', 'provider': 'musicbrainz',
            'tags': {'title': 'Original', 'artist': 'Artist'},
            'match': {'state': 'matching_metadata', 'reasons': []}}
        preserved['usage'] = provider_policy.candidate_usage(conn, preserved, time.time() - 30)
        old['candidates'].append(preserved)
        old['sources'].append({'provider': 'musicbrainz', 'state': 'provider_unavailable'})
        old['asr'] = {'language': 'ru', 'text': 'Existing synthetic independent draft'}
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(old), tid))
        usage_before = conn.execute("SELECT key,value FROM settings WHERE key GLOB 'recognition_usage:*'").fetchall()
        history_before = conn.execute('SELECT count(*) FROM history WHERE track_id=?', (tid,)).fetchone()[0]
    requests = []
    def transport(request):
        requests.append((request.url.host, request.url.path))
        assert request.url.host == 'lrclib.net', 'Unrelated provider was contacted'
        if status != 200:
            return httpx.Response(status, json={'code': 'TRACK_NOT_FOUND'} if status == 404 else {})
        return httpx.Response(200, json={'id': 1234, 'trackName': 'Original', 'artistName': 'Artist',
            'albumName': 'Confirmed album', 'duration': track['duration'],
            'syncedLyrics': '[00:00.100]Known fixture words', 'plainLyrics': 'Known fixture words'})
    async def unrelated(*args, **kwargs):
        raise AssertionError('Lyric-only preparation invoked cover/catalog enrichment')
    monkeypatch.setattr(catalogs, 'enrich', unrelated)
    monkeypatch.setattr(Providers, 'cover', unrelated)
    monkeypatch.setattr(worker, 'Providers', lambda settings, metrics=None:
                        Providers(settings, httpx.MockTransport(transport), metrics=metrics))
    begun = start(client, headers)
    saved_job = db.fetchone(cfg.db_path, 'SELECT * FROM jobs WHERE id=?', (begun['job_id'],))
    assert json.loads(saved_job['payload'])['items'][0]['lyric_search_only'] is True
    drain(cfg); prep.tick(cfg)
    job = client.get('/api/jobs/' + begun['job_id']).json()
    assert requests and {host for host, _ in requests} == {'lrclib.net'}
    assert job['state'] == ('succeeded' if status in {200, 404} else 'queued')
    if status in {429, 503}: assert job['stage'] == 'waiting_provider'
    data = json.loads(db.fetchone(cfg.db_path, 'SELECT payload FROM analyses WHERE track_id=?', (tid,))['payload'])
    kept = next(c for c in data['candidates'] if c['id'] == 'mb:kept')
    assert kept['usage'] == preserved['usage'], 'Other-source TTL was refreshed'
    assert data['asr'] == old['asr']
    assert next(s for s in data['sources'] if s['provider'] == 'musicbrainz')['state'] == 'provider_unavailable'
    with db.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT key,value FROM settings WHERE key GLOB 'recognition_usage:*'").fetchall() == usage_before
        assert conn.execute('SELECT count(*) FROM history WHERE track_id=?', (tid,)).fetchone()[0] == history_before
    assert media.sha256_file(path) == sha and not client.get('/api/tracks/' + tid).json()['lrc']
    if status == 200:
        assert any(c.get('provider') == 'lrclib' and c.get('timing_verified') is False for c in data['candidates'])
        assert client.get('/api/metadata-preparation').json()['state'] == 'review'
    else:
        assert not any(c.get('provider') == 'lrclib' and c.get('lrc') for c in data['candidates'])


def test_scope_flag_cannot_be_added_to_a_different_frozen_item(library):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    begun = start(client, headers)
    job = dict(db.fetchone(cfg.db_path, 'SELECT * FROM jobs WHERE id=?', (begun['job_id'],)))
    item = copy.deepcopy(json.loads(job['payload'])['items'][0])
    item['relative_path'] = 'other.flac'
    class Forbidden:
        settings = cfg
        async def io(self, fn, *args):
            return fn(*args)
        async def analyze_item(self, *args, **kwargs):
            raise AssertionError('Provider analysis ran before frozen-item validation')
    with pytest.raises(media.MediaError) as denied:
        asyncio.run(prep.search(Forbidden(), job, item))
    assert denied.value.code == 'preparation_invalid'
    assert media.sha256_file(path) == sha


def test_generic_job_cannot_enter_lyric_preparation_scope(library):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    item = {'track_id': tid, 'sha256': sha, 'relative_path': path.name, 'lyric_search_only': True}
    class Forbidden:
        settings = cfg
        async def io(self, *args):
            raise AssertionError('Non-preparation job reached persisted-run validation')
        async def analyze_item(self, *args, **kwargs):
            raise AssertionError('Non-preparation job reached provider')
    job = {'kind': 'analyze', 'payload': db.encode({'items': [item]})}
    with pytest.raises(media.MediaError) as denied:
        asyncio.run(prep.search(Forbidden(), job, item))
    assert denied.value.code == 'preparation_invalid'


def test_client_cannot_request_lyric_scope_outside_server_plan(library):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    item, = client.get('/api/confirmed-fields/plan').json()['items']
    response = client.post('/api/metadata-preparation',
        json={'tokens': {tid: item['preparation']['token']}, 'lyric_search_only': True},
        headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
    assert response.status_code == 422


def test_unflagged_existing_run_retains_full_metadata_search(library, monkeypatch):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    begun = start(client, headers)
    with db.transaction(cfg.db_path) as conn:
        run = prep.load(conn)
        for item in run['items']: item.pop('lyric_search_only')
        prep.save(conn, run)
        job = conn.execute('SELECT payload FROM jobs WHERE id=?', (begun['job_id'],)).fetchone()
        payload = json.loads(job[0]);payload['items'] = run['items']
        conn.execute('UPDATE jobs SET payload=? WHERE id=?', (db.encode(payload), begun['job_id']))
    seen = []
    async def metadata(self, job, item, **kwargs):
        assert not kwargs
        seen.append(item['track_id'])
        return {'state': 'no_match'}
    monkeypatch.setattr(worker.Worker, 'analyze_item', metadata)
    drain(cfg); prep.tick(cfg)
    assert seen == [tid] and client.get('/api/metadata-preparation').json()['state'] == 'review'


def test_lyric_query_does_not_fetch_embedded_release_cover_or_catalogs(library, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    requests = []
    def transport(request):
        requests.append(request.url.host)
        assert request.url.host == 'lrclib.net'
        return httpx.Response(200, json={'id': 9, 'trackName': 'Fixture', 'artistName': 'Artist',
            'albumName': 'Album', 'duration': 2, 'syncedLyrics': '[00:00.1]Known words'})
    async def unrelated(*args, **kwargs):
        raise AssertionError('Unrelated cover/catalog operation ran')
    monkeypatch.setattr(catalogs, 'enrich', unrelated)
    monkeypatch.setattr(Providers, 'cover', unrelated)
    meta = {'sha256': sha, 'tags': {'title': 'Fixture', 'artist': 'Artist', 'album': 'Album',
        'musicbrainz_albumid': '11111111-1111-4111-8111-111111111111'}, 'duration': 2, 'has_art': False}
    async def run():
        provider = Providers(cfg, httpx.MockTransport(transport))
        try:
            result = await provider.analyze(meta, 'fixture.flac', lyrics_only=True)
            assert {s['provider'] for s in result['sources']} == {'lrclib'}
            assert result['automatic_apply'] is False and result['candidates']
            with pytest.raises(media.MediaError):
                await provider.analyze(meta, 'fixture.flac', lyrics_only=True, album_hint='Other')
        finally: await provider.close()
    asyncio.run(run())
    assert requests == ['lrclib.net']
