from dataclasses import replace
import json
import time

from fastapi.testclient import TestClient
from phonoteka import db,webhooks
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings,client,authenticated,seed


def test_scopes_token_hash_only_and_revocation(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as owner:
        headers=authenticated(owner,settings);tid,sha=seed(settings)
        created=owner.post('/api/tokens',json={'name':'n8n','scopes':['read','jobs:analysis'],'days':7},headers=headers)
        assert created.status_code==201,created.text
        token=created.json();assert token['token'] not in settings.db_path.read_bytes().decode('latin1')
        assert token['token'] not in owner.get('/api/tokens').text
        with TestClient(create_app(settings),base_url=settings.app_url) as client:
            auth={'Authorization':'Bearer '+token['token']}
            assert client.get('/api/tracks',headers=auth).status_code==200
            assert client.get('/api/tokens',headers=auth).status_code==403
            denied=client.post('/api/jobs',json={'kind':'apply','track_ids':[tid]},headers={**auth,'Idempotency-Key':'no-write'})
            assert denied.status_code==403
            queued=client.post('/api/jobs',json={'kind':'audit','track_ids':[tid]},headers={**auth,'Idempotency-Key':'audit'})
            assert queued.status_code==202,queued.text
            jid=queued.json()['id']
            assert client.post('/api/tokens',json={'name':'new','scopes':['jobs:write']},headers=auth).status_code==403
            assert owner.delete('/api/tokens/'+token['id'],headers=headers).status_code==200
            assert client.get('/api/tracks',headers=auth).status_code==401
            assert owner.get('/api/jobs/'+jid).json()['state']=='cancelled'


def test_expiry_stops_pending_worker_effects_and_cross_origin(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as owner:
        headers=authenticated(owner,settings);tid,sha=seed(settings)
        token=owner.post('/api/tokens',json={'name':'test','scopes':['read','jobs:analysis']},headers=headers).json()
        auth={'Authorization':'Bearer '+token['token'],'Idempotency-Key':'expiry'}
        assert owner.get('/api/tracks',headers={**auth,'Origin':'https://other.example'}).status_code==403
        queued=owner.post('/api/jobs',json={'kind':'audit','track_ids':[tid]},headers=auth).json()
        worker=Worker(settings)
        try:
            assert not worker.cancelled(queued['id'])
            with db.transaction(settings.db_path) as conn:conn.execute('UPDATE api_tokens SET expires=? WHERE id=?',(time.time()-1,token['id']))
            assert worker.cancelled(queued['id'])
        finally:worker.pool.shutdown()
        assert owner.get('/api/tracks',headers=auth).status_code==401


def test_revocation_releases_queued_and_paused_notification_reservations_atomically(client,settings):
    headers=authenticated(client,settings);tid,_=seed(settings)
    # The app was started with notifications disabled, so its sender cannot
    # transmit. Only durable enqueue policy is enabled for this synthetic test.
    key=settings.state_dir/'synthetic-webhook-key';key.write_bytes(b'synthetic-notification-signing-key-123456')
    cfg=replace(settings,webhook_enabled=True,webhook_url='https://receiver.example/hook',
                webhook_allowed_hosts=('receiver.example',),webhook_secret_file=key)
    webhooks.configure(cfg)
    token=client.post('/api/tokens',json={'name':'cancel-test','scopes':['read','jobs:analysis']},headers=headers).json()
    ids=[]
    for index in range(3):
        response=client.post('/api/jobs',json={'kind':'audit','track_ids':[tid]},headers={'Authorization':'Bearer '+token['token'],'Idempotency-Key':f'revoke-{index}'})
        assert response.status_code==202;ids.append(response.json()['id'])
    with db.transaction(settings.db_path) as conn:
        conn.execute("UPDATE jobs SET state='paused',stage='paused',completed=1,total=2 WHERE id=?",(ids[1],))
        conn.execute("UPDATE jobs SET state='running',stage='audit' WHERE id=?",(ids[2],))
    assert webhooks.status(cfg)['reserved_jobs']==3
    assert client.delete('/api/tokens/'+token['id'],headers=headers).status_code==200
    assert client.get('/api/jobs/'+ids[0]).json()['state']=='cancelled'
    assert client.get('/api/jobs/'+ids[1]).json()['state']=='cancelled_partial'
    assert client.get('/api/jobs/'+ids[2]).json()['cancel_requested']
    status=webhooks.status(cfg)
    assert status['reserved_jobs']==1 and status['counts']['pending']==2
    with db.connect(settings.db_path) as conn:
        documents=[json.loads(row['payload']) for row in conn.execute('SELECT payload FROM webhook_outbox')]
    assert {item['job']['state'] for item in documents}=={'cancelled','cancelled_partial'}
    assert all(item['job']['code']=='authorization_revoked' for item in documents)
    assert token['token'] not in json.dumps(documents)
