from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import time

from fastapi.testclient import TestClient
import pytest

from phonoteka import db, exports, retention
from phonoteka.app import create_app
from test_backend import authenticated, job, seed, settings
from test_publication import stage_publication, work_version


def old_artifacts(settings):
    track, _ = seed(settings, 'retained.mp3')
    for index in range(4):
        work_version(settings, track, f'synthetic safe version {index}'.encode(), f'Version {index}')
        stage_publication(settings, track, review_version=index + 1)
    export_ids = [exports.generate(settings)['export_id'] for _ in range(3)]
    old = time.time() - 86400 * 30
    date = datetime.fromtimestamp(old, timezone.utc).isoformat()
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE history SET created_at=?', (date,))
    for identity in export_ids:
        path = settings.state_dir / 'exports' / identity / 'manifest.json'
        doc = json.loads(path.read_text()); doc['created_at'] = date
        path.write_text(json.dumps(doc))
    for folder in ('backups', 'exports'):
        for path in (settings.state_dir / folder).rglob('*'):
            os.utime(path, (old, old))
    return track, export_ids


def allow_cleanup(client, headers):
    previous = client.get('/api/retention').json()['policy']
    result = client.put('/api/retention/policy', json={'base_version': previous['version'], 'keep_all': False, 'min_age_days': 7, 'keep_last': 1}, headers=headers)
    assert result.status_code == 200, result.text


def test_keep_all_default_and_owner_policy_and_pin_revision_guards(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        assert client.get('/api/retention').status_code == 401
        headers = authenticated(client, settings)
        old_artifacts(settings)
        initial = retention.preview(settings, verify_files=True)
        assert initial['policy']['keep_all'] is True and initial['eligible_count'] == 0
        assert all('keep_all_policy' in item['protected_reasons'] for item in initial['objects'])
        bad = client.put('/api/retention/policy', json={'base_version': 0, 'keep_all': False, 'min_age_days': 7, 'keep_last': 0}, headers=headers)
        assert bad.status_code == 422
        allow_cleanup(client, headers)
        plan = retention.preview(settings, verify_files=True)
        candidate = next(item for item in plan['objects'] if item['eligible'] and item['kind'] == 'history')
        pin = {'base_version': 0, 'kind': 'history', 'identity': candidate['identity'], 'pinned': True, 'reason': 'Owner keeps this restoration point'}
        assert client.put('/api/retention/pins', json=pin).status_code == 403
        assert client.put('/api/retention/pins', json=pin, headers=headers).status_code == 200
        assert client.put('/api/retention/pins', json=pin, headers=headers).status_code == 412
        pinned = retention.preview(settings, verify_files=True)
        assert 'owner_pin' in next(item for item in pinned['objects'] if item['id'] == candidate['id'])['protected_reasons']
        with pytest.raises(ValueError, match='changed'):
            retention.apply(settings, plan, plan['plan_id'])
        assert all((settings.state_dir / path).exists() for path in candidate['files'])


def test_explicit_verified_prune_keeps_last_readable_work_ready_and_latest_export(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings); track, _ = old_artifacts(settings)
        allow_cleanup(client, headers)
        before = (settings.work_root / 'retained.mp3').read_bytes()
        ready = (settings.publication_root / 'Ready/retained.mp3').read_bytes()
        plan = retention.preview(settings, verify_files=True)
        assert plan['eligible_count'] > 0
        kept = [item for item in plan['objects'] if not item['eligible']]
        with pytest.raises(ValueError):
            retention.apply(settings, plan, 'not-an-approval')
        quick = retention.preview(settings)
        with pytest.raises(ValueError):
            retention.apply(settings, quick, quick['plan_id'])
        result = retention.apply(settings, plan, plan['plan_id'])
        assert result['removed'] and not result['music_deleted'] and not result['ready_deleted']
        assert (settings.work_root / 'retained.mp3').read_bytes() == before
        assert (settings.publication_root / 'Ready/retained.mp3').read_bytes() == ready
        assert all((settings.state_dir / path).exists() for item in kept for path in item['files'])
        history = client.get('/api/tracks/' + track).json()['history']
        pruned = [item for item in history if item['details'].get('backup_pruned')]
        assert pruned and all(not item['rollback_available'] for item in pruned)
        versions = client.get('/api/tracks/' + track + '/publication').json()['versions']
        assert all(not item['rollback_available'] for item in versions if item['backup_pruned'])
        with db.connect(settings.db_path) as conn:
            latest = conn.execute("SELECT value FROM settings WHERE key='latest_export'").fetchone()['value']
        assert (settings.state_dir / 'exports' / latest / 'manifest.json').exists()


def test_artifact_change_active_job_recovery_and_symlink_never_get_pruned(settings,tmp_path):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings); track, _ = old_artifacts(settings)
        allow_cleanup(client, headers)
        plan = retention.preview(settings, verify_files=True)
        candidate = next(item for item in plan['objects'] if item['eligible'] and item['kind'] == 'history')
        primary = settings.state_dir / candidate['primary_path']
        original = primary.read_bytes()
        retained_before = {path: (settings.state_dir / path).read_bytes() for item in plan['objects'] for path in item['files']}
        primary.write_bytes(b'changed external version')
        with pytest.raises(ValueError, match='changed|acquired protection'):
            retention.apply(settings, plan, plan['plan_id'])
        assert primary.read_bytes() == b'changed external version'
        assert all((settings.state_dir / path).read_bytes() == (b'changed external version' if path == candidate['primary_path'] else contents)
                   for path, contents in retained_before.items())
        primary.write_bytes(original)
        protected = tmp_path / 'outside-do-not-touch'; protected.write_bytes(b'outside secret fixture')
        primary.unlink(); primary.symlink_to(protected)
        guarded = retention.preview(settings, verify_files=True)
        obj = next(item for item in guarded['objects'] if item['id'] == candidate['id'])
        assert not obj['eligible'] and 'artifact_missing_or_unsafe' in obj['protected_reasons']
        assert protected.read_bytes() == b'outside secret fixture'
        primary.unlink(); primary.write_bytes(original)
        job(client, headers, 'audit', [track])
        active = retention.preview(settings, verify_files=True)
        assert all('active_job_reference' in item['protected_reasons'] for item in active['objects'] if item.get('track_id') == track)
        db.set_setting(settings.db_path, 'recovery_required', 'true')
        assert all('recovery_required' in item['protected_reasons'] for item in retention.preview(settings)['objects'])


def test_delivery_ttl_and_live_download_lock_protect_exports_and_receipt_releases_lease(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings); old_artifacts(settings); allow_cleanup(client, headers)
        plan = retention.preview(settings, verify_files=True)
        item = next(item for item in plan['objects'] if item['eligible'] and item['kind'] == 'export')
        started = client.post('/api/export-leases', json={'export_id': item['identity']}, headers=headers)
        assert started.status_code == 201, started.text
        lease = started.json()
        assert lease['expires_at'] - lease['created_at'] >= 1799
        assert client.post('/api/export-leases/' + lease['id'] + '/renew', headers=headers).status_code == 200
        leased = next(obj for obj in retention.preview(settings, verify_files=True)['objects'] if obj['id'] == item['id'])
        assert 'active_export_lease' in leased['protected_reasons']
        response = client.post('/api/deliveries', json={'export_id': item['identity'], 'device': 'test-copy', 'lease_id': lease['id']}, headers=headers)
        assert response.status_code == 201, response.text
        assert db.setting(settings.db_path, 'export_lease:' + lease['id']) is None
        with retention.export_lock(settings, item['identity']):
            unlocked_plan = retention.preview(settings, verify_files=True)
            result = retention.apply(settings, unlocked_plan, unlocked_plan['plan_id'])
            assert {'id': item['id'], 'reason': 'active_download_file_lock'} in result['skipped']
            assert (settings.state_dir / item['primary_path']).exists()
        doc = exports.manifest(settings, item['identity'])
        path = doc['files'][0]['path']
        from urllib.parse import quote
        data = client.get('/api/exports/' + item['identity'] + '/files/' + quote(path), headers={'Range':'bytes=0-3'})
        assert data.status_code == 206 and len(data.content) == 4
        assert not db.fetchall(settings.db_path, "SELECT key FROM settings WHERE key LIKE 'export_lease:%'")
