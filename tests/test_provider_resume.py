import asyncio
import json
import time
from dataclasses import replace
from fastapi.testclient import TestClient
from phonoteka import db
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings,authenticated,seed,job


def test_provider_wait_is_durable_does_not_count_complete_and_skips_done(settings,monkeypatch):
    cfg=replace(settings,external_fingerprints=True,file_workers=1)
    calls=[]
    async def analyze(self,j,item):
        calls.append(item['track_id'])
        if len(calls)==2:return {'state':'incomplete','retry_at':time.time()+600}
        return {'state':'candidates'}
    monkeypatch.setattr(Worker,'analyze_item',analyze)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);ids=[seed(cfg,str(i)+'.mp3')[0] for i in range(3)]
        queued=job(client,headers,'recognize',ids)
        first=Worker(cfg)
        asyncio.run(first.process(first.claim_job()));first.pool.shutdown()
        state=client.get('/api/jobs/'+queued['id']).json()
        assert state['state']=='queued' and state['stage']=='waiting_provider' and state['retry_at']>time.time()
        assert state['completed']==1 and state['failed']==0 and len(calls)==2
        second=Worker(cfg)
        assert second.claim_job() is None
        row=db.fetchone(cfg.db_path,'SELECT payload FROM jobs WHERE id=?',(queued['id'],));payload=json.loads(row['payload']);payload['retry_at']=0
        with db.transaction(cfg.db_path) as conn:conn.execute('UPDATE jobs SET payload=? WHERE id=?',(db.encode(payload),queued['id']))
        asyncio.run(second.process(second.claim_job()));second.pool.shutdown()
        state=client.get('/api/jobs/'+queued['id']).json()
        assert state['state']=='succeeded' and state['completed']==3
        assert calls==[ids[0],ids[1],ids[1],ids[2]]


def test_failed_asr_new_generation_success_reuses(settings):
    cfg=replace(settings,inference_url='http://inference:8001')
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);tid,sha=seed(cfg)
        first=job(client,headers,'transcribe',[tid])
        with db.transaction(cfg.db_path) as conn:conn.execute("UPDATE jobs SET state='failed' WHERE id=?",(first['id'],))
        second=job(client,headers,'transcribe',[tid])
        payload=json.loads(db.fetchone(cfg.db_path,'SELECT payload FROM jobs WHERE id=?',(second['id'],))['payload'])
        assert payload['items'][0]['generation']==1
        with db.transaction(cfg.db_path) as conn:conn.execute("UPDATE jobs SET state='succeeded' WHERE id=?",(second['id'],))
        third=job(client,headers,'transcribe',[tid])
        assert json.loads(db.fetchone(cfg.db_path,'SELECT payload FROM jobs WHERE id=?',(third['id'],))['payload'])['items'][0]['generation']==1
