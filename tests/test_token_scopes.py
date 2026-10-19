"""Canonical capability boundaries use the same HTTP routes as integrations."""
from dataclasses import replace
import json
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from phonoteka import automation, db, exports, media
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, job, drain, synthetic, accept_publication_review
from test_api_facades import ready_fixture
from test_exports import published


def issue(client, owner_headers, scopes):
    response = client.post('/api/tokens', json={'name': 'synthetic integration', 'scopes': scopes}, headers=owner_headers)
    assert response.status_code == 201, response.text
    token = response.json()
    return token, {'Authorization': 'Bearer ' + token['token']}


def enqueue(client, auth, kind, ids=(), params=None):
    return client.post('/api/jobs', json={'kind': kind, 'track_ids': list(ids), 'params': params or {}},
                       headers={**auth, 'Idempotency-Key': str(uuid.uuid4())})


def denied(response, code='scope_denied'):
    assert response.status_code == 403, response.text
    assert response.json()['code'] == code


def test_canonical_scopes_are_explicit_and_separate_from_owner_session(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        owner = authenticated(client, settings)
        tid, _ = seed(settings)
        token, read = issue(client, owner, ['library:read'])
        assert token['scopes'] == ['library:read']
        assert client.get('/api/v1/tracks', headers=read).status_code == 200
        denied(enqueue(client, read, 'audit', [tid]))
        # A valid owner cookie cannot amplify a restricted bearer credential.
        denied(client.get('/api/tokens', headers=read), 'owner_session_required')
        invalid = client.post('/api/tokens', json={'name': 'invalid', 'scopes': ['files:admin']}, headers=owner)
        assert invalid.status_code == 422 and invalid.json()['code'] == 'invalid_scopes'
        assert token['token'] not in client.get('/api/tokens').text
        assert token['token'] not in settings.db_path.read_bytes().decode('latin1')


def test_jobs_create_is_analysis_only_and_freezes_the_grant(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        tid, sha = seed(cfg)
        token, auth = issue(client, owner, ['jobs:create'])
        queued = enqueue(client, auth, 'audit', [tid])
        assert queued.status_code == 202, queued.text
        payload = json.loads(db.fetchone(cfg.db_path, 'SELECT payload FROM jobs WHERE id=?', (queued.json()['id'],))['payload'])
        assert payload['auth_token_id'] == token['id'] and payload['auth_scopes'] == ['jobs:create']
        denied(client.get('/api/tracks', headers=auth))
        for kind in ('apply', 'rollback', 'normalize_audio', 'extract_cue', 'publish', 'export_ready'):
            denied(enqueue(client, auth, kind, [] if kind == 'export_ready' else [tid]))
        for params in ({'acceptance_mode': 'automatic'}, {'profile': 'automatic'}, {'kind': 'apply'}):
            response = enqueue(client, auth, 'audit', [tid], params)
            assert response.status_code == 422, response.text
        denied(client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'forbidden'}}, headers={**auth, 'If-Match': sha}))
        assert client.post('/api/jobs/' + queued.json()['id'] + '/cancel', headers=auth).status_code == 200
        assert client.get('/api/jobs/' + queued.json()['id']).json()['state'] == 'cancelled'


def test_job_controls_check_the_existing_job_kind_not_only_enqueue_permission(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        tid, sha = seed(cfg)
        client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'Draft'}}, headers={**owner, 'If-Match': sha})
        queued = job(client, owner, 'apply', [tid])
        _, analysis = issue(client, owner, ['jobs:create'])
        for action in ('cancel', 'pause', 'resume', 'retry-storage'):
            denied(client.post(f'/api/jobs/{queued["id"]}/{action}', headers=analysis))
        assert client.get('/api/jobs/' + queued['id']).json()['state'] == 'queued'


def test_files_permission_edits_proposals_but_requires_job_permission_for_effects(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        tid, sha = seed(cfg)
        _, files = issue(client, owner, ['files:write'])
        saved = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'Draft'}}, headers={**files, 'If-Match': sha})
        assert saved.status_code == 201, saved.text
        proposal = saved.json()['id']
        edited = client.patch('/api/v1/proposals/' + proposal, json={'tags': {'title': 'Revised'}}, headers={**files, 'If-Match': sha})
        assert edited.status_code == 201, edited.text
        proposal = edited.json()['id']
        denied(enqueue(client, files, 'apply', [tid]))
        denied(enqueue(client, files, 'audit', [tid]))
        denied(client.post('/api/v1/proposals/' + proposal + '/apply', json={}, headers={**files, 'If-Match': sha, 'Idempotency-Key': 'files-alone'}))
        _, writer = issue(client, owner, ['jobs:create', 'files:write'])
        applied = client.post('/api/v1/proposals/' + proposal + '/apply', json={}, headers={**writer, 'If-Match': sha, 'Idempotency-Key': 'file-job'})
        assert applied.status_code == 202, applied.text
        denied(enqueue(client, writer, 'publish', [tid]))
        denied(enqueue(client, writer, 'export_ready'))
        denied(client.post('/api/publications/plan', json={'track_ids': [tid]}, headers={**writer, 'Idempotency-Key': 'no-plan'}))
        assert media.sha256_file(cfg.work_root / 'a.mp3') == sha


def test_publication_permission_plans_but_cannot_write_work(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        tid, sha = ready_fixture(client, cfg, owner)
        _, pub = issue(client, owner, ['publish:write'])
        planned = client.post('/api/v1/publications/plan', json={'track_ids': [tid]}, headers={**pub, 'Idempotency-Key': 'publication-only'})
        assert planned.status_code == 201, planned.text
        plan = planned.json()
        commit = '/api/v1/publications/' + plan['id'] + '/commit'
        denied(client.post(commit, json={'plan_sha256': plan['plan_sha256']}, headers={**pub, 'Idempotency-Key': 'not-a-job-grant'}))
        _, publisher = issue(client, owner, ['jobs:create', 'publish:write'])
        queued = client.post(commit, json={'plan_sha256': plan['plan_sha256']}, headers={**publisher, 'Idempotency-Key': 'publish-job'})
        assert queued.status_code == 202, queued.text
        assert enqueue(client, publisher, 'export_ready').status_code == 202
        denied(enqueue(client, publisher, 'apply', [tid]))
        denied(enqueue(client, publisher, 'rollback', [tid]))
        denied(client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'wrong-domain'}}, headers={**publisher, 'If-Match': sha}))
        assert not (cfg.publication_root / 'Ready/named.mp3').exists()


def test_export_leases_and_delivery_are_publication_capabilities_not_human_attestation(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        owner = authenticated(client, settings)
        published(settings)
        identity = exports.generate(settings)['export_id']
        _, files = issue(client, owner, ['files:write', 'jobs:create'])
        denied(client.post('/api/export-leases', json={'export_id': identity}, headers=files))
        denied(client.post('/api/deliveries', json={'export_id': identity, 'device': 'synthetic'}, headers=files))
        _, pub = issue(client, owner, ['publish:write'])
        leased = client.post('/api/export-leases', json={'export_id': identity}, headers=pub)
        assert leased.status_code == 201, leased.text
        lease = leased.json()['id']
        assert client.post(f'/api/export-leases/{lease}/renew', headers=pub).status_code == 200
        receipt = client.post('/api/deliveries', json={'export_id': identity, 'device': 'synthetic', 'lease_id': lease}, headers=pub)
        assert receipt.status_code == 201 and receipt.json()['verification_origin'] == 'api_client_report'
        assert receipt.json()['poweramp_checked'] is False
        physical = client.post('/api/deliveries', json={'export_id': identity, 'device': 'synthetic', 'poweramp_checked': True}, headers=pub)
        assert physical.status_code == 403 and physical.json()['code'] == 'owner_session_required'
        leased = client.post('/api/export-leases', json={'export_id': identity}, headers=pub).json()['id']
        assert client.delete('/api/export-leases/' + leased, headers=pub).status_code == 200
        denied(client.get('/api/exports/latest', headers=pub))


def test_all_canonical_scopes_cannot_change_owner_policies_or_human_reviews(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        owner = authenticated(client, settings)
        tid, sha = seed(settings)
        _, auth = issue(client, owner, sorted(automation.CANONICAL_SCOPES))
        assert client.get('/api/tracks', headers=auth).status_code == 200
        denied(client.post('/api/tokens', json={'name': 'escalate', 'scopes': ['publish:write']}, headers=auth), 'owner_session_required')
        denied(client.put('/api/schedule', json={}, headers=auth), 'owner_session_required')
        denied(client.put(f'/api/tracks/{tid}/recording-classification', json={'kind': 'single_recording', 'base_version': 0, 'reason': 'synthetic owner review'}, headers={**auth, 'If-Match': sha}), 'owner_session_required')
        review = client.get(f'/api/tracks/{tid}/lyrics-review').json()
        denied(client.put(f'/api/tracks/{tid}/lyrics-review', json={'base_version': review['version'], 'document_revision': review['document_revision']}, headers={**auth, 'If-Match': sha}), 'owner_session_required')


@pytest.mark.parametrize('scopes,analysis,export,write,read', [
    (['jobs:analysis'], True, True, False, False),
    (['jobs:write'], False, False, True, False),
    (['read'], False, False, False, True),
])
def test_legacy_aliases_retain_exact_existing_powers(settings, scopes, analysis, export, write, read):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        tid, sha = seed(cfg)
        _, auth = issue(client, owner, scopes)
        assert client.get('/api/tracks', headers=auth).status_code == (200 if read else 403)
        assert enqueue(client, auth, 'audit', [tid]).status_code == (202 if analysis else 403)
        assert enqueue(client, auth, 'export_ready').status_code == (202 if export else 403)
        proposal = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'Legacy draft'}}, headers={**auth, 'If-Match': sha})
        assert proposal.status_code == (201 if write else 403), proposal.text
        if write:
            assert enqueue(client, auth, 'apply', [tid]).status_code == 202
        else:
            denied(enqueue(client, auth, 'apply', [tid]))
        # New retry-storage permission is not silently added to old names.
        denied(client.post('/api/jobs/' + str(uuid.uuid4()) + '/retry-storage', headers=auth))


def test_worker_checks_live_and_accepted_grants_and_legacy_unsnapshotted_jobs(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        tid, sha = seed(cfg)
        client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'Accepted'}}, headers={**owner, 'If-Match': sha})
        token, auth = issue(client, owner, ['jobs:create', 'files:write'])
        queued = enqueue(client, auth, 'apply', [tid]).json()
        worker = Worker(cfg)
        try:
            row = db.fetchone(cfg.db_path, 'SELECT * FROM jobs WHERE id=?', (queued['id'],))
            worker.authorize(row)
            # Even a restored/changed token row cannot broaden the accepted grant.
            with db.transaction(cfg.db_path) as conn:
                conn.execute('UPDATE api_tokens SET scopes=? WHERE id=?', (db.encode(['jobs:create', 'publish:write']), token['id']))
            with pytest.raises(media.MediaError) as error:
                worker.authorize(row)
            assert error.value.code == 'authorization_scope_denied'
            assert not automation.token_grant_allowed(cfg, token_id=token['id'], auth_epoch=row['auth_epoch'], kind='publish', granted_scopes=['jobs:create', 'files:write'])
            assert not automation.token_grant_allowed(cfg, token_id=token['id'], auth_epoch=row['auth_epoch'], kind='publish')
            legacy, _ = issue(client, owner, ['jobs:analysis'])
            assert automation.token_grant_allowed(cfg, token_id=legacy['id'], auth_epoch=row['auth_epoch'], kind='export_ready')
            assert not automation.token_grant_allowed(cfg, token_id=legacy['id'], auth_epoch=row['auth_epoch'], kind='publish')
        finally:
            worker.pool.shutdown()
        assert media.sha256_file(cfg.work_root / 'a.mp3') == sha


def test_owner_resume_preserves_the_original_token_grant(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        owner = authenticated(client, settings)
        tid, _ = seed(settings)
        token, auth = issue(client, owner, ['jobs:create'])
        queued = enqueue(client, auth, 'audit', [tid]).json()
        paused = client.post('/api/jobs/' + queued['id'] + '/pause', headers=auth)
        assert paused.status_code == 200, paused.text
        resumed = client.post('/api/jobs/' + queued['id'] + '/resume', headers=owner)
        assert resumed.status_code == 200, resumed.text
        payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM jobs WHERE id=?', (queued['id'],))['payload'])
        assert payload['auth_token_id']==token['id'] and payload['auth_scopes']==['jobs:create']
        assert resumed.json()['completed'] == 0 and resumed.json()['total'] == 1


@pytest.mark.parametrize('malformed', ['not json', '{}', '["jobs:create", "unknown"]', [], ['files:write', 3], None])
def test_unknown_or_malformed_grants_fail_closed(malformed):
    assert not automation.job_scope_allowed(malformed, 'audit')
    assert not automation.job_scope_allowed(malformed, 'apply')
    assert not automation.has_scope(malformed, 'files:write')


@pytest.mark.skipif(sys.platform != 'linux', reason='Durable native writes require Linux')
def test_scoped_native_apply_and_publication_use_the_actual_worker(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    path = synthetic(cfg)
    original_sha = media.sha256_file(path)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        job(client, owner, 'scan'); drain(cfg)
        track = client.get('/api/tracks').json()['items'][0]
        tid = track['id']
        _, writer = issue(client, owner, ['jobs:create', 'files:write'])
        proposal = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'Scoped native update'}}, headers={**writer, 'If-Match': track['sha256']})
        assert proposal.status_code == 201, proposal.text
        applied = enqueue(client, writer, 'apply', [tid])
        assert applied.status_code == 202, applied.text
        drain(cfg)
        assert client.get('/api/jobs/' + applied.json()['id']).json()['state'] == 'succeeded'
        changed = client.get('/api/tracks/' + tid).json()
        assert changed['sha256'] != original_sha and changed['tags']['title'] == 'Scoped native update'
        assert media.sha256_file(cfg.source_root / path.name) == original_sha
        accept_publication_review(client, owner, tid)
        _, publisher = issue(client, owner, ['jobs:create', 'publish:write'])
        publication = enqueue(client, publisher, 'publish', [tid])
        assert publication.status_code == 202, publication.text
        drain(cfg)
        assert client.get('/api/jobs/' + publication.json()['id']).json()['state'] == 'succeeded'
        assert media.sha256_file(cfg.publication_root / 'Ready' / path.name) == changed['sha256']
        denied(enqueue(client, publisher, 'rollback', [tid]))
        work_history = db.fetchone(cfg.db_path, "SELECT id FROM history WHERE track_id=? AND kind='apply'", (tid,))
        ready_history = db.fetchone(cfg.db_path, "SELECT id FROM history WHERE track_id=? AND kind='publish'", (tid,))
        for auth, history in ((publisher, work_history), (writer, ready_history)):
            denied(client.post('/api/v1/revisions/' + history['id'] + '/rollback', headers={**auth, 'If-Match': changed['sha256'], 'Idempotency-Key': str(uuid.uuid4())}))
        rolled_back = enqueue(client, writer, 'rollback', [tid])
        assert rolled_back.status_code == 202, rolled_back.text
        drain(cfg)
        assert client.get('/api/jobs/' + rolled_back.json()['id']).json()['state'] == 'succeeded'
        assert media.sha256_file(path) == original_sha
