"""Real journal/copy/rollback effects on isolated synthetic bytes, no music API."""
from dataclasses import replace
import json
import uuid

from fastapi.testclient import TestClient

from phonoteka import db, media, publication
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import authenticated, drain, job, seed, settings


def work_version(settings, track_id, data, title):
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
    path = settings.work_root / row['relative_path']
    path.write_bytes(data)
    sha = media.sha256_file(path)
    metadata = json.loads(row['metadata'])
    metadata['sha256'] = sha
    metadata['tags']['title'] = [title]
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE tracks SET sha256=?,metadata=? WHERE id=?', (sha, db.encode(metadata), track_id))
    return sha


def stage_publication(settings, track_id, *, review_version=1, persist=True):
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
    item = {'track_id': track_id, 'sha256': row['sha256'], 'relative_path': row['relative_path']}
    identity = str(uuid.uuid4())
    now = db.utcnow()
    payload = {'items': [item], 'params': {}}
    with db.transaction(settings.db_path) as conn:
        conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,'publish',?,'processing',?,1,?,?,1)",
                     (identity, 'succeeded' if persist else 'running', db.encode(payload), now, now))
    operation = {'id': identity, 'kind': 'publish', 'payload': db.encode(payload)}
    gate = {'state': 'passed', 'profile': 'relaxed', 'review_version': review_version, 'document_revision': 'd' * 64,
            'exceptions': ['synthetic_transaction_fixture'], 'exception_reason': 'Synthetic Ready journal fixture'}
    audit = {'state': 'passed', 'sha256': row['sha256']}
    intent = publication.publish_intent(settings, operation, item, row, gate, audit)
    result = media.publish(settings.work_root / row['relative_path'], settings.publication_root / 'Ready' / row['relative_path'],
                           row['sha256'], settings.roots, settings.publication_root,
                           expected_destination_sha256=intent['before']['sha256'] if intent['before'] else None,
                           backup_dir=settings.state_dir / 'backups' / track_id / identity)
    if persist:
        publication.persist(settings, operation, item, result)
    return operation, item, result


def baseline(settings):
    track_id, _ = seed(settings, 'ready.mp3')
    first = work_version(settings, track_id, b'synthetic Ready version one', 'Version one')
    first_job, _, first_result = stage_publication(settings, track_id)
    second = work_version(settings, track_id, b'synthetic Ready version two', 'Version two')
    second_job, _, second_result = stage_publication(settings, track_id, review_version=2)
    third = work_version(settings, track_id, b'new working version remains independent', 'Working three')
    return track_id, first, second, third, first_job, first_result, second_job, second_result


def test_ready_rollback_keeps_new_work_revision_and_can_itself_be_reversed(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        track, first, second, third, _, _, _, second_result = baseline(settings)
        work = settings.work_root / 'ready.mp3'
        ready = settings.publication_root / 'Ready/ready.mp3'
        before_meta = json.loads(db.fetchone(settings.db_path, 'SELECT metadata FROM tracks WHERE id=?', (track,))['metadata'])
        selected = job(client, headers, 'rollback_ready', [track], {'history_id': second_result['operation_id'], 'expected_ready_sha256': second})
        drain(settings)
        status = client.get('/api/jobs/' + selected['id']).json()
        assert status['state'] == 'succeeded', status
        assert media.sha256_file(ready) == first
        assert media.sha256_file(work) == third
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track,))
        meta = json.loads(row['metadata'])
        assert row['sha256'] == third and meta['tags'] == before_meta['tags']
        assert meta['publication']['sha256'] == first and meta['publication']['review_version'] == 1
        plan = client.get(f'/api/tracks/{track}/publication?verify=true').json()
        assert plan['stale_ready'] and plan['destination_integrity'] == 'matches'
        assert plan['work_revision'] == third and plan['ready_revision'] == first
        stale = client.get('/api/publication/plan?stale_only=true').json()
        assert stale['total'] == 1 and stale['automatic_deletion'] is False and stale['phone_changes'] is False
        inverse = db.fetchone(settings.db_path, "SELECT id FROM history WHERE track_id=? AND kind='rollback_ready'", (track,))['id']
        reverse = job(client, headers, 'rollback_ready', [track], {'history_id': inverse, 'expected_ready_sha256': first})
        drain(settings)
        assert client.get('/api/jobs/' + reverse['id']).json()['state'] == 'succeeded'
        assert media.sha256_file(ready) == second and media.sha256_file(work) == third


def test_external_ready_change_after_queue_is_preserved_and_conflicts(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        track, _, second, third, _, _, _, second_result = baseline(settings)
        queued = job(client, headers, 'rollback_ready', [track], {'history_id': second_result['operation_id'], 'expected_ready_sha256': second})
        target = settings.publication_root / 'Ready/ready.mp3'
        target.write_bytes(b'owner external change must survive')
        changed = media.sha256_file(target)
        drain(settings)
        assert client.get('/api/jobs/' + queued['id']).json()['state'] == 'conflict'
        assert media.sha256_file(target) == changed
        assert media.sha256_file(settings.work_root / 'ready.mp3') == third
        assert client.get(f'/api/tracks/{track}/publication?verify=true').json()['destination_integrity'] == 'conflict'


def test_first_publication_has_no_absent_baseline_delete_and_requires_owner(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        track, sha = seed(settings, 'first.mp3')
        _, _, result = stage_publication(settings, track)
        data = {'kind': 'rollback_ready', 'track_ids': [track], 'params': {'history_id': result['operation_id'], 'expected_ready_sha256': sha}}
        response = client.post('/api/jobs', json=data, headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
        assert response.status_code == 409 and response.json()['code'] == 'ready_rollback_unavailable'
        assert (settings.publication_root / 'Ready/first.mp3').exists()
        token = client.post('/api/tokens', json={'name': 'automation', 'scopes': ['read', 'jobs:write']}, headers=headers).json()['token']
        response = client.post('/api/jobs', json=data, headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': str(uuid.uuid4())})
        assert response.status_code == 403
        client.post('/api/auth/logout', headers=headers)
        assert client.get('/api/publication/plan').status_code == 401


def test_committed_ready_journal_recovers_frozen_profile_without_rewriting_work(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url):
        track, _ = seed(settings, 'recovery.mp3')
        stage_publication(settings, track)
        sha = work_version(settings, track, b'committed second Ready', 'Newest work metadata')
        queued, _, effect = stage_publication(settings, track, review_version=7, persist=False)
        assert not db.fetchone(settings.db_path, 'SELECT id FROM history WHERE id=?', (effect['operation_id'],))
        worker = Worker(settings)
        try:
            worker.recover_jobs()
        finally:
            worker.pool.shutdown()
        assert db.fetchone(settings.db_path, 'SELECT state FROM jobs WHERE id=?', (queued['id'],))['state'] == 'succeeded'
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track,))
        meta = json.loads(row['metadata'])
        assert row['sha256'] == sha and media.sha256_file(settings.work_root / 'recovery.mp3') == sha
        assert meta['tags']['title'] == ['Newest work metadata']
        assert meta['publication']['review_version'] == 7 and meta['publication']['profile'] == 'relaxed'
        assert meta['publication']['lyrics_gate']['exception_reason'] == 'Synthetic Ready journal fixture'
