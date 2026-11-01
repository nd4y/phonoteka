"""Retries resolve the original request, and pending work cannot starve behind done rows."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient
from phonoteka import db
from phonoteka.app import create_app
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
