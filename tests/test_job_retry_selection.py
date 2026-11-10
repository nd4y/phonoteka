"""Retries resolve the original request, and pending work cannot starve behind done rows."""
import asyncio
from dataclasses import replace
import json

from fastapi.testclient import TestClient
from phonoteka import db
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed


def test_pending_request_replays_after_all_items_complete(settings):
    settings = replace(settings, external_fingerprints=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = {**authenticated(client, settings), 'Idempotency-Key': 'retry-original'}
        tid, sha = seed(settings)
        body = {'kind': 'recognize', 'params': {'scope': 'pending'}}
        first = client.post('/api/jobs', json=body, headers=headers)
        assert first.status_code == 202
        with db.transaction(settings.db_path) as conn:
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (tid, sha, db.encode({
                'sources': [{'provider': 'shazam', 'state': 'no_match'}]}), db.utcnow()))
            conn.execute("UPDATE jobs SET state='completed' WHERE id=?", (first.json()['id'],))
        again = client.post('/api/jobs', json=body, headers=headers)
        assert again.status_code == 202, again.text
        assert again.json()['id'] == first.json()['id']
        assert again.json()['state'] == 'completed'
        conflict = client.post('/api/jobs', json={**body, 'track_ids': [tid]}, headers=headers)
        assert conflict.status_code == 409 and conflict.json()['code'] == 'idempotency_conflict'
        fresh = client.post('/api/jobs', json=body, headers={**headers, 'Idempotency-Key': 'fresh'})
        assert fresh.status_code == 409 and fresh.json()['code'] == 'nothing_to_process'
        assert db.fetchone(settings.db_path, 'SELECT count(*) AS n FROM jobs')['n'] == 1


def test_pending_selection_applies_limit_after_completed_files(settings):
    settings = replace(settings, external_fingerprints=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = {**authenticated(client, settings), 'Idempotency-Key': 'pending-tail'}
        sha, now = 'a' * 64, db.utcnow()
        meta = db.encode({'format': 'mp3', 'tags': {}, 'write_supported': True})
        with db.transaction(settings.db_path) as conn:
            conn.executemany('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                [(f'file-{i:05}', f'{i:05}.mp3', sha, meta, now, now) for i in range(10002)])
            conn.executemany('INSERT INTO analyses VALUES(?,?,?,?)',
                [(f'file-{i:05}', sha, db.encode({'sources': [{'provider': 'shazam', 'state': 'no_match'}]}), now) for i in range(10000)])
        response = client.post('/api/jobs', json={'kind': 'recognize', 'params': {'scope': 'pending'}}, headers=headers)
        assert response.status_code == 202, response.text
        payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM jobs WHERE id=?', (response.json()['id'],))['payload'])
        assert [item['track_id'] for item in payload['items']] == ['file-10000', 'file-10001']
        assert response.json()['total'] == 2


def test_active_job_lookup_finds_old_paused_task_after_recent_history(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        with db.transaction(settings.db_path) as conn:
            conn.executemany("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,?,?,?,?,0,?,?,1)",
                [('old-paused', 'scan', 'paused', 'paused', '{}', '2026-01-01', '2026-01-01')]
                + [(f'done-{i:03}', 'scan', 'completed', 'done', '{}', '2026-02-01', '2026-02-01') for i in range(51)])
        assert all(job['id'] != 'old-paused' for job in client.get('/api/jobs').json()['items'])
        active = client.get('/api/jobs?active_only=true&kind=scan&page_size=1')
        assert active.status_code == 200
        assert active.json()['total'] == 1
        assert [job['id'] for job in active.json()['items']] == ['old-paused']
        assert client.get('/api/jobs?active_only=true&kind=recognize').json()['total'] == 0
        assert client.get('/api/jobs').json()['total'] == 52
        visible = client.get('/api/jobs?active_first=true').json()
        assert visible['items'][0]['id'] == 'old-paused'
        assert len(visible['items']) == 50 and visible['total'] == 52


def test_pending_music_filters_select_only_matching_incomplete_recordings(settings):
    settings = replace(settings, external_metadata=True, external_fingerprints=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        sha, now = 'a' * 64, db.utcnow()
        tags = {'title': 'Song', 'artist': 'Artist', 'album': 'Album'}
        timed = '[00:01.00] A line'
        base = {'format': 'mp3', 'tags': tags, 'has_art': True, 'lrc': timed}
        variants = {
            'need-cover': {**base, 'has_art': False},
            'need-timing': {**base, 'lrc': ''},
            'need-tag': {**base, 'tags': {'artist': 'Artist', 'album': 'Album'}},
            'complete': base,
            'mix': {**base, 'possible_mix': True, 'has_art': False},
            'speech': {**base, 'tags': {**tags, 'genre': ['Podcast']}, 'has_art': False},
            'cue-image': {**base, 'cue_album_image': True, 'has_art': False},
            'done-search': {**base, 'has_art': False},
        }
        with db.transaction(settings.db_path) as conn:
            conn.executemany('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                             [(key, key + '.mp3', sha, db.encode(value), now, now) for key, value in variants.items()])
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', ('done-search', sha, db.encode({
                'sources': [{'provider': p, 'state': 'no_match'} for p in ('shazam', 'musicbrainz', 'lrclib')]}), now))

        def queue(kind, scope):
            response = client.post('/api/jobs', json={'kind': kind, 'params': {'scope': scope}},
                                   headers={**headers, 'Idempotency-Key': kind + '-' + scope})
            assert response.status_code == 202, response.text
            job = db.fetchone(settings.db_path, 'SELECT payload FROM jobs WHERE id=?', (response.json()['id'],))
            return json.loads(job['payload'])['items']

        cover_item, = queue('analyze', 'music_missing_art')
        assert cover_item['track_id'] == 'need-cover'
        assert cover_item['music_gap_filter'] == 'music_missing_art'
        for kind, scope, expected in [
            ('analyze', 'music_gaps', {'need-cover', 'need-timing', 'need-tag'}),
            ('recognize', 'music_missing_lyrics', {'need-timing'}),
            ('recognize', 'music_missing_tags', {'need-tag'}),
        ]:
            items = queue(kind, scope)
            assert {item['track_id'] for item in items} == expected
            assert all(item['music_gap_filter'] == scope and item['sha256'] == sha for item in items)
        assert {'complete', 'mix', 'speech'} <= {item['track_id'] for item in queue('analyze', 'pending')}

        invalid = client.post('/api/jobs', json={'kind': 'replaygain', 'params': {'scope': 'music_gaps'}},
                              headers={**headers, 'Idempotency-Key': 'wrong-kind'})
        assert invalid.status_code == 422 and invalid.json()['code'] == 'invalid_scope'

        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(variants['mix']), 'need-cover'))
        worker = Worker(settings)
        try:
            result = asyncio.run(worker.analyze_item({'kind': 'analyze'}, cover_item))
        finally:
            worker.pool.shutdown()
        assert result == {'skipped': True, 'reason': 'music_gap_no_longer_matches', 'sha256': sha}
