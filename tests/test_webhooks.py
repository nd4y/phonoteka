import asyncio
from dataclasses import replace
import json
import socket
import time

import httpx
import pytest

from phonoteka import db, webhooks
from test_backend import authenticated, client, settings


def config(settings, capacity=10):
    db.initialize(settings.db_path)
    key=settings.state_dir/'webhook-test-key'
    key.write_bytes(b'only-a-synthetic-unit-test-signing-key-123')
    cfg=replace(settings,webhook_enabled=True,webhook_url='https://receiver.example/private-endpoint',
                webhook_allowed_hosts=('receiver.example',),webhook_secret_file=key,webhook_max_pending=capacity)
    assert webhooks.configure(cfg)['enabled']
    return cfg


def create(cfg,identity='job-one'):
    with db.transaction(cfg.db_path) as conn:
        stamp=db.utcnow()
        conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at) VALUES(?,'scan','queued','pending','{}',3,?,?)",(identity,stamp,stamp))
        db.event(conn,'job_created',{'job_id':identity,'kind':'scan','title':'never-in-webhook'})


def finish(cfg,identity='job-one',state='succeeded'):
    with db.transaction(cfg.db_path) as conn:
        conn.execute('UPDATE jobs SET state=?,stage=?,completed=3 WHERE id=?',(state,state,identity))
        db.event(conn,'job_finished',{'job_id':identity,'state':state,'path':'/private/music','lyrics':'never-in-webhook','token':'never-in-webhook'})


def row(cfg):return db.fetchone(cfg.db_path,'SELECT * FROM webhook_outbox ORDER BY event_id LIMIT 1')


def test_outbox_result_and_event_are_atomic_and_payload_is_allowlisted(settings):
    cfg=config(settings);create(cfg)
    with pytest.raises(RuntimeError):
        with db.transaction(cfg.db_path) as conn:
            conn.execute("UPDATE jobs SET state='succeeded' WHERE id='job-one'")
            db.event(conn,'job_finished',{'job_id':'job-one','lyrics':'secret'})
            raise RuntimeError('simulate failed commit')
    assert row(cfg) is None
    assert db.fetchone(cfg.db_path,"SELECT state FROM jobs WHERE id='job-one'")['state']=='queued'
    finish(cfg)
    item=row(cfg);document=json.loads(item['payload'])
    assert document['id']==item['id'] and document['job']['completed']==3
    assert '/private/music' not in item['payload'] and 'never-in-webhook' not in item['payload']
    assert db.fetchone(cfg.db_path,'SELECT COUNT(*) AS n FROM webhook_reservations')['n']==0
    with db.transaction(cfg.db_path) as conn:db.event(conn,'job_finished',{'job_id':'job-one'})
    assert db.fetchone(cfg.db_path,'SELECT COUNT(*) AS n FROM webhook_outbox')['n']==1


def test_capacity_reserves_terminal_slot_without_losing_existing_job_result(settings):
    cfg=config(settings,capacity=2);create(cfg,'one');create(cfg,'two')
    with pytest.raises(webhooks.OutboxFull):create(cfg,'three')
    assert db.fetchone(cfg.db_path,"SELECT id FROM jobs WHERE id='three'") is None
    finish(cfg,'one');finish(cfg,'two')
    assert db.fetchone(cfg.db_path,'SELECT COUNT(*) AS n FROM webhook_outbox')['n']==2
    assert db.fetchone(cfg.db_path,"SELECT COUNT(*) AS n FROM jobs WHERE state='succeeded'")['n']==2


def test_sender_retries_same_id_and_body_hmac_dns_pinning_and_no_music_repeat(settings):
    cfg=config(settings);create(cfg);finish(cfg)
    requests=[]
    def handle(request):
        requests.append(request)
        assert request.url.host=='8.8.8.8' and request.headers['Host']=='receiver.example'
        assert request.extensions['sni_hostname']=='receiver.example'
        assert webhooks.verify_signature(webhooks.secret(cfg),request.content,request.headers['X-Phonoteka-Timestamp'],request.headers['X-Phonoteka-Event-ID'],request.headers['X-Phonoteka-Signature'])
        return httpx.Response(503 if len(requests)==1 else 204)
    async def run():
        sender=webhooks.Sender(cfg,httpx.MockTransport(handle),resolver=lambda *_:'8.8.8.8')
        assert await sender.once() and row(cfg)['state']=='pending'
        with db.transaction(cfg.db_path) as conn:conn.execute('UPDATE webhook_outbox SET next_attempt=0')
        assert await sender.once() and row(cfg)['state']=='delivered'
    asyncio.run(run())
    assert requests[0].content==requests[1].content
    assert requests[0].headers['Idempotency-Key']==requests[1].headers['Idempotency-Key']
    assert db.fetchone(cfg.db_path,"SELECT completed,state FROM jobs WHERE id='job-one'")=={'completed':3,'state':'succeeded'}


def test_lease_recovery_is_cas_and_unknown_outcomes_have_bounded_attempts(settings):
    cfg=config(settings);create(cfg);finish(cfg)
    first=webhooks.claim(cfg,now=100);second=webhooks.claim(cfg,now=131)
    assert first['id']==second['id'] and first['lease_token']!=second['lease_token']
    assert webhooks.complete(cfg,first,'delivered',True,now=132)=='lease_lost'
    assert row(cfg)['state']=='in_flight' and row(cfg)['attempts']==2
    for attempt in range(3,webhooks.MAX_ATTEMPTS+1):
        last=webhooks.claim(cfg,now=100+31*(attempt-1))
        assert last['attempts']==attempt
    assert webhooks.claim(cfg,now=100+31*webhooks.MAX_ATTEMPTS) is None
    assert row(cfg)['state']=='dead' and row(cfg)['last_code']=='delivery_unknown_after_lease'


def test_disabled_or_paused_or_recovery_never_transmits_and_reserved_finish_is_retained(settings):
    cfg=config(settings);create(cfg)
    webhooks.configure(replace(cfg,webhook_enabled=False))
    finish(cfg)
    assert row(cfg)['state']=='pending'
    assert webhooks.claim(cfg) is None
    webhooks.configure(cfg)
    db.set_setting(cfg.db_path,'webhook_paused','true')
    assert webhooks.claim(cfg) is None
    db.set_setting(cfg.db_path,'webhook_paused','false');db.set_setting(cfg.db_path,'recovery_required','true')
    assert webhooks.claim(cfg) is None
    assert webhooks.status(cfg)['recovery_suspended']


def test_destination_policy_rejects_private_dns_and_redirect_without_follow(settings,monkeypatch):
    cfg=config(settings)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_args,**_kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError):webhooks.resolve('receiver.example',True)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_args,**_kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.168.0.10',443))])
    with pytest.raises(ValueError):webhooks.resolve('receiver.example')
    assert webhooks.resolve('receiver.example',True)=='192.168.0.10'
    assert not webhooks.configure(replace(cfg,webhook_url='https://receiver.example/hook?secret=forbidden'))['enabled']
    webhooks.configure(cfg);create(cfg);finish(cfg)
    requests=[]
    def redirect(request):requests.append(request);return httpx.Response(302,headers={'Location':'http://127.0.0.1/secret'})
    asyncio.run(webhooks.Sender(cfg,httpx.MockTransport(redirect),resolver=lambda *_:'8.8.8.8').once())
    assert len(requests)==1 and row(cfg)['state']=='dead' and row(cfg)['last_code']=='redirect_rejected'


def test_signature_replay_window_and_tampering():
    key=b'synthetic-key'*4;body=b'{"id":"stable-id"}'
    signature=webhooks.signature(key,body,1000,'stable-id')
    assert webhooks.verify_signature(key,body,'1000','stable-id',signature,now=1001)
    assert not webhooks.verify_signature(key,body,'1000','stable-id',signature,now=1301)
    assert not webhooks.verify_signature(key,body+b' ','1000','stable-id',signature,now=1001)
    assert not webhooks.verify_signature(key,body,'1000','another-id',signature,now=1001)


def test_owner_status_redacts_path_and_secret_retry_keeps_event_id(client,settings):
    assert client.get('/api/webhooks').status_code==401
    headers=authenticated(client,settings);cfg=config(settings)
    create(cfg);finish(cfg)
    item=webhooks.claim(cfg);webhooks.complete(cfg,item,'http_403',permanent=True)
    response=client.get('/api/webhooks')
    assert response.status_code==200 and response.json()['counts']['dead']==1
    assert 'private-endpoint' not in response.text and str(cfg.webhook_secret_file) not in response.text
    assert webhooks.secret(cfg).decode() not in response.text
    endpoint='/api/webhooks/'+item['id']+'/retry'
    assert client.post(endpoint).status_code==403
    retried=client.post(endpoint,headers=headers)
    assert retried.status_code==200 and retried.json()['id']==item['id']
    assert row(cfg)['attempts']==0 and row(cfg)['state']=='pending'
    assert client.post('/api/webhooks/pause',headers=headers).status_code==200
    assert client.post('/api/webhooks/resume',headers=headers).status_code==200


def test_retention_removes_only_old_terminal_notifications(settings):
    cfg=config(settings)
    for identity in ('old-terminal','old-pending','new-terminal'):
        create(cfg,identity);finish(cfg,identity)
    with db.transaction(cfg.db_path) as conn:
        conn.execute("UPDATE webhook_outbox SET state='dead',updated_at='2000-01-01T00:00:00+00:00' WHERE job_id='old-terminal'")
        conn.execute("UPDATE webhook_outbox SET updated_at='2000-01-01T00:00:00+00:00',next_attempt=? WHERE job_id='old-pending'",(time.time()+86400,))
    item=webhooks.claim(cfg)
    assert item['job_id']=='new-terminal'
    webhooks.complete(cfg,item,'delivered',delivered=True)
    assert db.fetchone(cfg.db_path,"SELECT id FROM webhook_outbox WHERE job_id='old-terminal'") is None
    assert db.fetchone(cfg.db_path,"SELECT state FROM webhook_outbox WHERE job_id='old-pending'")['state']=='pending'


def test_epoch_rotation_affects_only_new_notifications_and_event_pruning_keeps_outbox(settings):
    cfg=config(settings);create(cfg);finish(cfg)
    original=row(cfg)
    with db.transaction(cfg.db_path) as conn:
        conn.execute('DELETE FROM events')
        conn.execute("UPDATE settings SET value='new-restore-epoch' WHERE key='webhook_event_epoch'")
    create(cfg,'new-job');finish(cfg,'new-job')
    result=db.fetchone(cfg.db_path,"SELECT * FROM webhook_outbox WHERE job_id='new-job'")
    assert result['id'].startswith('new-restore-epoch:')
    assert row(cfg)['id']==original['id'] and row(cfg)['payload']==original['payload']
