import json
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from phonoteka import db,maintenance,media
from phonoteka.app import create_app
from phonoteka.worker import scheduler_guard
from test_backend import settings,authenticated,synthetic,job,drain


def test_backup_restore_checks_artifacts_revokes_access_and_blocks_effects(settings,tmp_path):
    synthetic(settings)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        issued=client.post('/api/tokens',json={'name':'backup-test','scopes':['read']},headers=headers).json()
        backup=tmp_path/'snapshot';result=maintenance.backup(settings,backup)
        assert result['verified'] and not result['music_included']
        restored=tmp_path/'restored';maintenance.restore(backup,restored)
        cfg=replace(settings,state_dir=restored)
        assert db.setting(cfg.db_path,'recovery_required')=='true'
        assert not db.fetchall(cfg.db_path,'SELECT * FROM sessions') and not db.fetchall(cfg.db_path,'SELECT * FROM api_tokens')
        with TestClient(create_app(cfg),base_url=cfg.app_url) as recovered:
            new=authenticated(recovered,cfg)
            response=recovered.post('/api/jobs',headers={**new,'Idempotency-Key':'denied'},json={'kind':'analyze','params':{'scope':'all'}})
            assert response.status_code==409 and response.json()['code']=='recovery_required'
        path=cfg.work_root/'sample.flac';before=path.read_bytes();path.write_bytes(before+b'changed')
        with pytest.raises(media.MediaError):maintenance.acknowledge(cfg)
        assert db.setting(cfg.db_path,'recovery_required')=='true'
        path.write_bytes(before);assert maintenance.acknowledge(cfg)['recovery_required'] is False
        with (backup/'phonoteka.sqlite3').open('ab') as output:output.write(b'corrupt')
        with pytest.raises(ValueError,match='hash'):maintenance.restore(backup,tmp_path/'bad-restore')
        assert not (tmp_path/'bad-restore').exists()


def test_backup_requires_scheduler_stop_and_disjoint_new_destination(settings,tmp_path):
    with TestClient(create_app(settings),base_url=settings.app_url):
        with scheduler_guard(settings.state_dir):
            with pytest.raises(RuntimeError):maintenance.backup(settings,tmp_path/'snapshot')
        with pytest.raises(ValueError,match='overlap'):maintenance.backup(settings,settings.work_root/'snapshot')
        with pytest.raises(ValueError,match='exist'):maintenance.backup(settings,settings.work_root)


def test_pause_resume_preserves_job_and_cancel_route(settings):
    synthetic(settings)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        tid=client.get('/api/tracks').json()['items'][0]['id'];queued=job(client,headers,'audit',[tid])
        assert client.post('/api/jobs/'+queued['id']+'/pause',headers=headers).json()['state']=='paused'
        drain(settings);assert client.get('/api/jobs/'+queued['id']).json()['completed']==0
        assert client.post('/api/jobs/'+queued['id']+'/resume',headers=headers).json()['state']=='queued'
        drain(settings);assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        other=job(client,headers,'audit',[tid])
        assert client.post('/api/jobs/'+other['id']+'/cancel',headers=headers).status_code==200


def test_staging_cleanup_preview_keeps_new_and_protected_data(settings):
    import os,time
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        root=settings.work_root/'.phonoteka-staging';root.mkdir()
        old=root/'cue-old';old.mkdir();(old/'scratch').write_text('temporary')
        new=root/'normalize-new';new.mkdir();(new/'scratch').write_text('new')
        foreign=root/'owner-files';foreign.mkdir();(foreign/'keep').write_text('keep')
        before=time.time()-86400*8;os.utime(old,(before,before));os.utime(foreign,(before,before))
        assert maintenance.cleanup_staging(settings)['candidates']==['cue-old'] and old.exists()
        result=maintenance.cleanup_staging(settings,apply=True)
        assert result['removed']==1 and not old.exists() and new.exists() and foreign.exists()
        headers=authenticated(client,settings)
        diagnostic=client.get('/api/reports/operations');assert diagnostic.status_code==200
        assert str(settings.work_root) not in diagnostic.text and 'test-strong-owner' not in diagnostic.text
        assert diagnostic.json()['rollback_retention']=='keep_all'
