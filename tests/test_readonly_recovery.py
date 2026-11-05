"""A restart pauses unfinished reads without replay or phantom running items."""
import asyncio
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pytest

from phonoteka import db
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, job


@pytest.mark.parametrize('cancelled', [False, True])
def test_restart_preserves_completed_and_provider_budget_until_explicit_resume(settings, monkeypatch, cancelled):
    cfg = replace(settings, external_metadata=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client,cfg)
        ids = [seed(cfg,relative=str(i)+'.mp3')[0] for i in range(3)]
        queued = job(client,headers,'analyze',ids)
        with db.transaction(cfg.db_path) as conn:
            conn.execute("UPDATE jobs SET state='running',stage='running',completed=1,cancel_requested=? WHERE id=?", (int(cancelled),queued['id']))
            conn.execute('INSERT INTO job_items VALUES(?,?,?,?)', (queued['id'],ids[0],'succeeded','{"kept":true}'))
            conn.execute('INSERT INTO job_items VALUES(?,?,?,?)', (queued['id'],ids[1],'running','{}'))
            conn.execute('INSERT INTO settings VALUES(?,?)', ('recognition_usage:fixture','{"calls":3}'))
            conn.execute('INSERT INTO settings VALUES(?,?)', ('provider_attempt:fixture','{"state":"sent"}'))
        worker = Worker(cfg)
        try:
            worker.recover_jobs()
            worker.recover_jobs()  # Paused/cancelled jobs are not recovered again.
            state = client.get('/api/jobs/'+queued['id']).json()
            assert state['state'] == ('cancelled_partial' if cancelled else 'paused')
            assert state['completed'] == 1
            assert db.fetchone(cfg.db_path,"SELECT COUNT(*) AS n FROM job_items WHERE state='running'")['n'] == 0
            assert db.setting(cfg.db_path,'recognition_usage:fixture') == '{"calls":3}'
            assert db.setting(cfg.db_path,'provider_attempt:fixture') == '{"state":"sent"}'
            original = db.fetchone(cfg.db_path,'SELECT result FROM job_items WHERE job_id=? AND item_key=?',(queued['id'],ids[0]))
            assert json.loads(original['result']) == {'kept':True}
            if cancelled:
                return
            called = []
            async def analyze(_job,item):
                called.append(item['track_id'])
                return {'state':'no_match'}
            monkeypatch.setattr(worker,'analyze_item',analyze)
            assert client.post('/api/jobs/'+queued['id']+'/resume',headers=headers,json={}).status_code == 200
            current = dict(db.fetchone(cfg.db_path,'SELECT * FROM jobs WHERE id=?',(queued['id'],)))
            asyncio.run(worker.process(current))
            assert set(called) == set(ids[1:]) and len(called) == 2
            assert client.get('/api/jobs/'+queued['id']).json()['state'] == 'succeeded'
        finally:
            worker.pool.shutdown(wait=True)
