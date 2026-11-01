"""Named API03 routes exercise the same immutable proposals/jobs and Ready CAS."""
from dataclasses import replace
import json
import uuid

from fastapi.testclient import TestClient

from phonoteka import db, media, publication
from phonoteka.app import create_app
from test_backend import settings, authenticated, seed, accept_publication_review
from test_publication import stage_publication, work_version


def ready_fixture(client, settings, headers, name='named.mp3'):
    identity, sha = seed(settings, name)
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone()
        metadata = json.loads(row['metadata'])
        # Synthetic journal fixtures isolate API/revision behavior from decoding.
        metadata['audit'] = {'state': 'passed', 'sha256': sha}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), identity))
    accept_publication_review(client, headers, identity)
    return identity, sha


def plan(client, headers, ids, key='plan'):
    result = client.post('/api/v1/publications/plan', json={'track_ids': ids, 'profile': 'relaxed'}, headers={**headers, 'Idempotency-Key': key})
    assert result.status_code == 201, result.text
    return result.json()


def test_named_proposal_patch_is_immutable_and_apply_reuses_idempotency(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        tid, sha = seed(settings)
        base = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'First'}}, headers={**headers, 'If-Match': sha}).json()
        route = '/api/v1/proposals/' + base['id']
        assert client.patch(route, json={'tags': {'title': 'Second'}}, headers=headers).status_code == 428
        changed = client.patch(route, json={'tags': {'title': 'Second'}}, headers={**headers, 'If-Match': sha})
        assert changed.status_code == 201, changed.text
        new = changed.json()
        assert new['id'] != base['id']
        old = db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (base['id'],))
        assert json.loads(old['payload'])['tags']['title'] == 'First'
        assert client.patch(route, json={'tags': {'title': 'Third'}}, headers={**headers, 'If-Match': sha}).status_code == 412
        apply = '/api/v1/proposals/' + new['id'] + '/apply'
        assert client.post(apply, json={}, headers=headers).status_code == 428
        call_headers = {**headers, 'If-Match': sha, 'Idempotency-Key': 'named-apply'}
        queued = client.post(apply, json={}, headers=call_headers)
        assert queued.status_code == 202, queued.text
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('a' * 64, tid))
        assert client.post(apply, json={}, headers=call_headers).json()['id'] == queued.json()['id']
        assert client.post(apply, json={'acceptance_mode': 'automatic'}, headers=call_headers).status_code == 409
        assert client.post(apply, json={'verified': True}, headers=call_headers).status_code == 422


def test_named_plan_freezes_members_review_and_ready_and_replay_survives_effect(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        tid, sha = ready_fixture(client, settings, headers)
        before = media.sha256_file(settings.work_root / 'named.mp3')
        value = plan(client, headers, [tid])
        assert value['items'][0]['action'] == 'add' and value['multi_file_atomic'] is False
        assert plan(client, headers, [tid])['id'] == value['id']
        assert not (settings.publication_root / 'Ready/named.mp3').exists()
        assert media.sha256_file(settings.work_root / 'named.mp3') == before
        url = f'/api/v1/publications/{value["id"]}/commit'
        call_headers = {**headers, 'Idempotency-Key': 'named-commit'}
        assert client.post(url, json={'plan_sha256': '0' * 64}, headers=call_headers).status_code == 409
        queued = client.post(url, json={'plan_sha256': value['plan_sha256']}, headers=call_headers)
        assert queued.status_code == 202, queued.text
        payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM jobs WHERE id=?', (queued.json()['id'],))['payload'])
        assert payload['items'][0]['publication_target'] == value['items'][0]['publication_target']
        stage_publication(settings, tid)
        # Same accepted request resolves to its job even after Ready was committed.
        replay = client.post(url, json={'plan_sha256': value['plan_sha256']}, headers=call_headers)
        assert replay.status_code == 202 and replay.json()['id'] == queued.json()['id']
        assert client.post(url, json={'plan_sha256': value['plan_sha256']}, headers={**headers, 'Idempotency-Key': 'new-commit'}).status_code == 409
        assert client.get('/api/v1/publications/' + value['id']).json()['plan_sha256'] == value['plan_sha256']


def test_named_publication_plan_rejects_changed_other_member_before_any_effect(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        one, _ = ready_fixture(client, settings, headers, 'one.mp3')
        two, _ = ready_fixture(client, settings, headers, 'two.mp3')
        value = plan(client, headers, [one, two])
        (settings.work_root / 'two.mp3').write_bytes(b'changed outside application')
        response = client.post(f'/api/publications/{value["id"]}/commit', json={'plan_sha256': value['plan_sha256']}, headers={**headers, 'Idempotency-Key': 'stale-group'})
        assert response.status_code == 409, response.text
        assert not (settings.publication_root / 'Ready/one.mp3').exists()
        assert not db.fetchone(settings.db_path, "SELECT id FROM jobs WHERE kind='publish'")


def test_ordinary_publish_target_context_detects_catalog_change_not_just_work_revision(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        tid, _ = seed(settings)
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (tid,))
        frozen = publication.freeze_target(row)
        publication.assert_target(settings, row, frozen)
        stage_publication(settings, tid)
        fresh = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (tid,))
        try:
            publication.assert_target(settings, fresh, frozen)
        except media.MediaError as exc:
            assert exc.code == 'conflict'
        else:
            raise AssertionError('An old plan must not adopt a newer Ready revision')


def test_named_ready_rollback_uses_history_id_and_keeps_work(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        tid, _ = seed(settings)
        first = work_version(settings, tid, b'first ready', 'First')
        stage_publication(settings, tid)
        second = work_version(settings, tid, b'second ready', 'Second')
        _, _, result = stage_publication(settings, tid)
        third = work_version(settings, tid, b'independent work', 'Third')
        url = '/api/v1/revisions/' + result['operation_id'] + '/rollback'
        assert client.post(url, headers={**headers, 'Idempotency-Key': 'no-revision'}).status_code == 428
        response = client.post(url, headers={**headers, 'Idempotency-Key': 'named-ready-rollback', 'If-Match': second})
        assert response.status_code == 202, response.text
        value = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM jobs WHERE id=?', (response.json()['id'],))['payload'])
        assert value['items'][0]['expected_ready_sha256'] == second
        assert value['items'][0]['sha256'] == third
        assert media.sha256_file(settings.work_root / 'a.mp3') == third
        assert first != second


def test_facade_token_scopes_do_not_bypass_write_permissions(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        tid, sha = seed(settings)
        base = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'Owner'}}, headers={**headers, 'If-Match': sha}).json()
        token = client.post('/api/tokens', json={'name': 'read', 'scopes': ['read']}, headers=headers).json()['token']
        bearer = {'Authorization': 'Bearer ' + token, 'If-Match': sha, 'Idempotency-Key': str(uuid.uuid4())}
        assert client.patch('/api/v1/proposals/' + base['id'], json={'tags': {'title': 'Forged'}}, headers=bearer).status_code == 403
        assert client.post('/api/v1/proposals/' + base['id'] + '/apply', json={}, headers=bearer).status_code == 403
        assert client.post('/api/v1/publications/plan', json={'track_ids': [tid]}, headers=bearer).status_code == 403
