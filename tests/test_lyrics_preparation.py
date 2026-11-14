"""Real API admission and durable orchestration for lyric-only stale searches."""
import json
import time
import uuid

import pytest

from phonoteka import db, field_policy, media, metadata_preparation as prep, provider_policy
from phonoteka.worker import Worker
from test_backend import drain, job
from test_confirmed_fields import library, plan, enqueue
from test_metadata_preparation import settings, entry
from test_provider_policy import grant, lyrics


def lyric_only(library, *, stale=False):
    cfg, client, headers, tid, sha, path = library
    assert enqueue(client, headers, plan(client)).status_code == 202
    drain(cfg)
    for _ in range(2):
        job(client, headers, 'scan'); drain(cfg)
    track = client.get('/api/tracks/' + tid).json()
    assert track['has_art'] and track['tags']['album'] and not track['lrc']
    with db.transaction(cfg.db_path) as conn:
        grant(conn)
        payload = provider_policy.prepare_analysis(conn, {
            'candidates': [lyrics()], 'sources': [{'provider': 'lrclib', 'state': 'candidates'}],
        }, now=time.time() - 7200)
        conn.execute('UPDATE analyses SET input_sha256=?,payload=?,updated_at=? WHERE track_id=?',
                     ('0' * 64 if stale else track['sha256'], db.encode(payload), db.utcnow(), tid))
        identity = track['audit']['audio_identity'] if track.get('audit') else json.loads(
            conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()[0])['audit']['audio_identity']
        conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                     ('recognition_usage:' + identity, db.encode({'calls': 3})))
    return cfg, client, headers, tid, track['sha256'], path


@pytest.mark.parametrize('stale', [False, True])
def test_expired_or_stale_lyric_only_plan_is_metadata_only_and_read_only(library, stale):
    cfg, client, headers, tid, sha, path = lyric_only(library, stale=stale)
    before = db.fetchall(cfg.db_path, 'SELECT key,value FROM settings ORDER BY key')
    value = entry(client)
    assert not value['ready'] and {v['field'] for v in value['remaining']} == {'synced_lyrics'}
    assert value['preparation']['kind'] == 'analyze'
    assert value['preparation']['fingerprint_requests_remaining'] is None
    assert 'текста' in value['preparation']['reason']
    assert entry(client)['preparation']['token'] == value['preparation']['token']
    assert db.fetchall(cfg.db_path, 'SELECT key,value FROM settings ORDER BY key') == before
    assert media.sha256_file(path) == sha


def test_refresh_keeps_expended_allowance_and_audio_and_new_words_need_review(library, monkeypatch):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    seen = []
    before = db.fetchall(cfg.db_path, "SELECT key,value FROM settings WHERE key GLOB 'recognition_usage:*'")
    writes = db.fetchone(cfg.db_path, "SELECT count(*) AS n FROM jobs WHERE kind IN ('apply','publish')")['n']
    async def search(self, job, item):
        assert job['kind'] == 'analyze'
        seen.append(item['track_id'])
        with db.transaction(cfg.db_path) as conn:
            data = provider_policy.prepare_analysis(conn, {'candidates': [lyrics()], 'sources': []})
            conn.execute('UPDATE analyses SET payload=?,input_sha256=?,updated_at=? WHERE track_id=?',
                         (db.encode(data), sha, db.utcnow(), tid))
        return {'state': 'candidates'}
    monkeypatch.setattr(Worker, 'analyze_item', search)
    value = entry(client)
    key = str(uuid.uuid4())
    body = {'tokens': {tid: value['preparation']['token']}}
    h = {**headers, 'Idempotency-Key': key}
    started = client.post('/api/metadata-preparation', json=body, headers=h)
    assert started.status_code == 202, started.text
    assert started.json()['stage'] == 'search'
    assert client.get('/api/jobs/' + started.json()['job_id']).json()['kind'] == 'analyze'
    assert client.post('/api/metadata-preparation', json=body, headers=h).json()['id'] == key
    drain(cfg); prep.tick(cfg)
    assert seen == [tid]
    assert client.get('/api/metadata-preparation').json()['state'] == 'review'
    assert entry(client)['preparation'] is None
    assert db.fetchall(cfg.db_path, "SELECT key,value FROM settings WHERE key GLOB 'recognition_usage:*'") == before
    assert db.fetchone(cfg.db_path, "SELECT count(*) AS n FROM jobs WHERE kind IN ('apply','publish')")['n'] == writes
    assert not client.get('/api/tracks/' + tid).json()['lrc']
    assert media.sha256_file(path) == sha


@pytest.mark.parametrize('previous_kind', ['analyze', 'recognize', 'exhausted-cache', 'lyrics-refresh'])
def test_existing_receipts_never_become_replay_permissions(library, previous_kind):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    with db.transaction(cfg.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        signature = [prep.CONTRACT, tid, sha, 'analyze' if previous_kind in {'analyze', 'lyrics-refresh'} else 'recognize', prep.guards(conn, row)]
        if previous_kind == 'exhausted-cache': signature.append(prep.CACHE_FALLBACK_PROFILE)
        if previous_kind == 'lyrics-refresh': signature.append(prep.LYRICS_REFRESH_PROFILE)
        conn.execute('INSERT INTO settings VALUES(?,?)', ('metadata_prepared:' + prep.fields.digest(signature), db.encode({'run_id': str(uuid.uuid4())})))
        before = conn.execute('SELECT count(*) FROM jobs').fetchone()[0]
    assert entry(client)['preparation'] is None
    assert db.fetchone(cfg.db_path, 'SELECT count(*) AS n FROM jobs')['n'] == before
    assert media.sha256_file(path) == sha


@pytest.mark.parametrize('stop', ['cancel', 'failure'])
def test_new_refresh_cannot_repeat_after_cancel_or_failed_search(library, monkeypatch, stop):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    async def failed(self, job, item):
        raise media.MediaError('provider_unavailable', 'Synthetic metadata lookup failure')
    monkeypatch.setattr(Worker, 'analyze_item', failed)
    value = entry(client)
    started = client.post('/api/metadata-preparation', json={'tokens': {tid: value['preparation']['token']}},
                          headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
    assert started.status_code == 202, started.text
    if stop == 'cancel':
        response = client.post('/api/metadata-preparation/cancel', json={'id': started.json()['id']}, headers=headers)
        assert response.status_code == 200
    drain(cfg); prep.tick(cfg)
    assert client.get('/api/metadata-preparation').json()['state'] == 'stopped'
    assert entry(client)['preparation'] is None
    assert media.sha256_file(path) == sha


@pytest.mark.parametrize('restriction', ['fetch', 'cache', 'embed', 'fresh', 'draft', 'plain', 'lock', 'instrumental', 'mix', 'speech', 'unknown', 'cooldown'])
def test_restricted_known_or_unaccepted_data_is_not_refreshed(library, restriction):
    cfg, client, headers, tid, sha, path = lyric_only(library)
    with db.transaction(cfg.db_path) as conn:
        if restriction in {'fetch', 'cache', 'embed'}:
            grant(conn, **{'allow_' + restriction: False})
        elif restriction == 'fresh':
            data = provider_policy.prepare_analysis(conn, {'candidates': [lyrics()], 'sources': []})
            conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(data), tid))
        elif restriction == 'draft':
            conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('new-draft', tid, sha, db.encode({'tags': {}, 'lrc': lyrics()['lrc']}), db.utcnow()))
        elif restriction == 'lock':
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('track_field_policy:' + tid, db.encode({'version': 1, 'locks': {'lrc': {'input_sha256': sha, 'value_sha256': field_policy.value_hash('')}}})))
        elif restriction == 'instrumental':
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('lyrics_review:' + tid, db.encode({'instrumental_confirmed': True})))
        elif restriction in {'unknown', 'cooldown'}:
            data = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()[0])
            data['sources'] = [{'provider': 'lrclib', 'state': 'unknown_outcome' if restriction == 'unknown' else 'provider_cooldown'}]
            conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(data), tid))
        else:
            meta = json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()[0])
            if restriction == 'plain': meta['lyrics'] = 'Existing known words'
            if restriction == 'mix': meta['possible_mix'] = True
            if restriction == 'speech': meta['tags']['genre'] = ['Spoken Word']
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
    assert entry(client)['preparation'] is None
    assert media.sha256_file(path) == sha
