from dataclasses import replace
import json
import time
from fastapi.testclient import TestClient
from phonoteka import db,scheduling,webhooks
from phonoteka.app import create_app
from test_backend import settings,client,authenticated,synthetic,drain,job


def test_schedule_resolves_stable_new_files_after_two_scans_and_deduplicates(settings):
    cfg=replace(settings,external_fingerprints=True,input_grace_seconds=0)
    synthetic(cfg)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg)
        state=client.put('/api/schedule',json={'enabled':True,'interval_minutes':30,'steps':['scan','recognize']},headers=headers).json()
        scheduling.tick(cfg,now=state['next_run']-1)
        assert client.get('/api/jobs').json()['total']==0
        scheduling.tick(cfg,now=state['next_run']+1);scheduling.tick(cfg,now=state['next_run']+2)
        assert client.get('/api/jobs').json()['total']==1
        # Stop the scheduler helper from queuing the next stage inside drain;
        # the normal loop only ticks once before draining the scan.
        drain(cfg)
        scheduling.tick(cfg,now=state['next_run']+3)
        assert db.fetchone(cfg.db_path,"SELECT * FROM jobs WHERE kind='recognize'") is None
        waiting=json.loads(db.setting(cfg.db_path,'schedule'))
        assert waiting['waiting_inputs']==1 and waiting['waiting_reason']=='input_stabilizing'
        job(client,headers,'scan');drain(cfg)
        # A later normal cycle sees two independently completed observations.
        db.set_setting(cfg.db_path,'schedule',db.encode({**waiting,'next_run':0,'remaining':['recognize']}))
        scheduling.tick(cfg)
        queued=db.fetchone(cfg.db_path,"SELECT * FROM jobs WHERE kind='recognize'")
        assert queued['total']==1 and json.loads(queued['payload'])['items'][0]['relative_path']=='sample.flac'
        client.put('/api/schedule',json={'enabled':False,'steps':['scan']},headers=headers)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='cancelled'


def test_schedule_disables_after_owner_auth_epoch_change(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings)
        state=client.put('/api/schedule',json={'enabled':True,'steps':['scan']},headers=headers).json()
        db.set_setting(settings.db_path,'auth_epoch','100')
        scheduling.tick(settings,now=state['next_run']+1)
        stored=json.loads(db.setting(settings.db_path,'schedule'))
        assert stored['enabled'] is False and stored['disabled_reason']=='authorization_changed'


def test_disable_schedule_cancels_waiting_jobs_with_durable_notifications(client,settings):
    headers=authenticated(client,settings)
    key=settings.state_dir/'synthetic-webhook-key';key.write_bytes(b'synthetic-notification-signing-key-123456')
    cfg=replace(settings,webhook_enabled=True,webhook_url='https://receiver.example/hook',
                webhook_allowed_hosts=('receiver.example',),webhook_secret_file=key)
    webhooks.configure(cfg)  # App sender uses original disabled settings.
    with db.transaction(settings.db_path) as conn:
        for identity,state,completed in [('queued-schedule','queued',0),('paused-schedule','paused',1),('running-schedule','running',0)]:
            stamp=db.utcnow()
            conn.execute('INSERT INTO jobs(id,kind,state,stage,payload,total,completed,created_at,updated_at) VALUES(?,?,?,?,?,2,?,?,?)',
                         (identity,'scan',state,state,db.encode({'trigger':'schedule'}),completed,stamp,stamp))
            db.event(conn,'job_created',{'job_id':identity,'kind':'scan'})
    response=client.put('/api/schedule',json={'enabled':False,'steps':['scan']},headers=headers)
    assert response.status_code==200
    assert client.get('/api/jobs/queued-schedule').json()['state']=='cancelled'
    assert client.get('/api/jobs/paused-schedule').json()['state']=='cancelled_partial'
    assert client.get('/api/jobs/running-schedule').json()['cancel_requested']
    status=webhooks.status(cfg)
    assert status['reserved_jobs']==1 and status['counts']['pending']==2
    with db.connect(settings.db_path) as conn:
        notices=[json.loads(row['payload']) for row in conn.execute('SELECT payload FROM webhook_outbox')]
        events=conn.execute("SELECT COUNT(*) FROM events WHERE kind='job_cancel_requested'").fetchone()[0]
    assert events==3 and all(item['job']['code']=='schedule_disabled' for item in notices)
