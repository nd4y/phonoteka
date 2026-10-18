from dataclasses import replace
import json
import time
from fastapi.testclient import TestClient
from phonoteka import db,scheduling
from phonoteka.app import create_app
from test_backend import settings,authenticated,synthetic,drain


def test_schedule_resolves_new_files_after_scan_and_deduplicates(settings):
    cfg=replace(settings,external_fingerprints=True)
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
