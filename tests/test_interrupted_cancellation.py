"""Cancellation closes an interrupted batch only after startup recovery."""
from dataclasses import replace
import json
import time

from fastapi.testclient import TestClient
import pytest

from phonoteka import db
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, job


def interrupted(client, cfg, headers):
    ids = [seed(cfg, relative=f'{n}.mp3')[0] for n in range(2)]
    queued = job(client, headers, 'audit', ids)
    with db.transaction(cfg.db_path) as conn:
        conn.execute("UPDATE jobs SET kind='publish',state='interrupted',stage='interrupted',completed=1,error='internal_error' WHERE id=?", (queued['id'],))
        conn.execute('INSERT INTO job_items VALUES(?,?,?,?)', (queued['id'], ids[0], 'succeeded', '{"committed_before_restart":true}'))
    return queued['id'], ids


def test_cancel_after_real_startup_recovery_preserves_completed_effects(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        identity, ids = interrupted(client, cfg, headers)
        original = [(cfg.work_root/f'{n}.mp3').read_bytes() for n in range(2)]
        endpoint = f'/api/jobs/{identity}/cancel'
        assert client.post(endpoint, headers=headers).status_code == 409
        worker = Worker(cfg)
        try:
            worker.recover_jobs()
            # The marker alone cannot prove that startup finished draining writes.
            assert client.post(endpoint, headers=headers).json()['code'] == 'job_recovery_pending'
            worker.state = 'idle'
            worker.heartbeat()
            response = client.post(endpoint, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()['state'] == 'cancelled_partial'
            assert response.json()['completed'] == 1 and response.json()['failed'] == 0
            assert client.post(endpoint, headers=headers).json()['state'] == 'cancelled_partial'
            items = db.fetchall(cfg.db_path, 'SELECT item_key,state,result FROM job_items WHERE job_id=?', (identity,))
            assert len(items) == 1 and items[0]['item_key'] == ids[0] and items[0]['state'] == 'succeeded'
            assert json.loads(items[0]['result']) == {'committed_before_restart': True}
            assert [(cfg.work_root/f'{n}.mp3').read_bytes() for n in range(2)] == original
            assert not list(cfg.publication_root.iterdir())
        finally:
            worker.pool.shutdown(wait=True)


@pytest.mark.parametrize('condition', ['not_recovered', 'stale', 'before_reconciliation', 'starting', 'stopped', 'readonly_worker', 'other_instance', 'restored_database'])
def test_cancel_cannot_acknowledge_an_unrecovered_or_live_interruption(settings, condition):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        identity, _ = interrupted(client, cfg, headers)
        heartbeat = {'at': time.time(), 'state': 'idle', 'read_only': False, 'instance_id': cfg.instance_id}
        marker = 'internal_error' if condition == 'not_recovered' else 'worker_restarted'
        if condition == 'stale': heartbeat['at'] -= 60
        if condition == 'before_reconciliation': heartbeat['at'] -= 1
        if condition in {'starting', 'stopped'}: heartbeat['state'] = condition
        if condition == 'readonly_worker': heartbeat['read_only'] = True
        if condition == 'other_instance': heartbeat['instance_id'] = 'another-instance'
        with db.transaction(cfg.db_path) as conn:
            conn.execute('UPDATE jobs SET error=? WHERE id=?', (marker, identity))
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('worker_heartbeat', db.encode(heartbeat)))
            if condition == 'restored_database':
                conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('recovery_required', 'true'))
        response = client.post(f'/api/jobs/{identity}/cancel', headers=headers)
        assert response.status_code == 409 and response.json()['code'] == 'job_recovery_pending'
        row = db.fetchone(cfg.db_path, 'SELECT state,cancel_requested,completed FROM jobs WHERE id=?', (identity,))
        assert row['state'] == 'interrupted' and row['cancel_requested'] == 0 and row['completed'] == 1
