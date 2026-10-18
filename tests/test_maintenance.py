import json
from pathlib import Path
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from phonoteka import db,maintenance,media,retention
from phonoteka.app import create_app
from phonoteka.worker import scheduler_guard
from test_backend import settings,authenticated,synthetic,job,drain


def test_backup_restore_checks_artifacts_revokes_access_and_blocks_effects(settings,tmp_path):
    synthetic(settings)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        issued=client.post('/api/tokens',json={'name':'backup-test','scopes':['read']},headers=headers).json()
        with db.transaction(settings.db_path) as conn:
            conn.execute('INSERT INTO provider_cache VALUES(?,?,?,?)',('private-cache','test','private-cache-marker-do-not-export',9999999999))
        backup=tmp_path/'snapshot';result=maintenance.backup(settings,backup)
        assert result['verified'] and not result['music_included']
        assert b'private-cache-marker-do-not-export' not in (backup/'phonoteka.sqlite3').read_bytes()
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


def test_restored_rollback_uses_copied_blob_after_original_state_blob_is_removed(settings,tmp_path):
    cfg=replace(settings,read_only=False,storage_verified=True)
    path=synthetic(cfg); original=path.read_bytes()
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        track=client.get('/api/tracks').json()['items'][0]
        response=client.post('/api/tracks/'+track['id']+'/proposal',headers={**headers,'If-Match':track['sha256']},json={'tags':{'title':'Restorable title'}})
        assert response.status_code==201,response.text
        draft=response.json()
        job(client,headers,'apply',[track['id']],{'proposal_id':draft['id']});drain(cfg)
        before=db.fetchone(cfg.db_path,"SELECT * FROM history WHERE track_id=? AND kind='apply'",(track['id'],))
        source_details=json.loads(before['details'])
        snapshot=tmp_path/'portable-snapshot';maintenance.backup(cfg,snapshot)
        # Verify compatibility with v1 snapshots made before source_state_dir.
        manifest_path=snapshot/'manifest.json';manifest=json.loads(manifest_path.read_text())
        manifest.pop('source_state_dir',None);manifest_path.write_text(json.dumps(manifest))
        restored=tmp_path/'portable-restored';maintenance.restore(snapshot,restored)
        restored_cfg=replace(cfg,state_dir=restored)
        details=json.loads(db.fetchone(restored_cfg.db_path,'SELECT details FROM history WHERE id=?',(before['id'],))['details'])
        assert details['backup_path'] != source_details['backup_path']
        copied=restored/'backups'/track['id']
        assert Path(details['backup_path']).is_relative_to(copied)
        journal=json.loads(Path(details['journal_path']).read_text())
        assert journal['backup'] == details['backup_path']
        Path(source_details['backup_path']).unlink()
        assert maintenance.acknowledge(restored_cfg)['recovery_required'] is False
        with TestClient(create_app(restored_cfg),base_url=cfg.app_url) as recovered:
            auth=authenticated(recovered,restored_cfg)
            visible=recovered.get('/api/tracks/'+track['id']).json()['history']
            assert next(row for row in visible if row['id']==before['id'])['rollback_available']
            rolled=job(recovered,auth,'rollback',[track['id']],{'history_id':before['id']});drain(restored_cfg)
            assert recovered.get('/api/jobs/'+rolled['id']).json()['state']=='succeeded'
        assert path.read_bytes()==original


def test_retention_journal_is_verified_and_restored_with_state(settings,tmp_path):
    cfg=replace(settings,read_only=False,storage_verified=True)
    with TestClient(create_app(cfg),base_url=cfg.app_url):
        # Exercise the real maintenance journal without deleting an artifact.
        plan=retention.preview(cfg,verify_files=True)
        retention.apply(cfg,plan,plan['plan_id'])
        relative='retention/'+plan['plan_id']+'.json'
        original=(cfg.state_dir/relative).read_bytes()
        assert json.loads(original)['state']=='committed'
        snapshot=tmp_path/'retention-snapshot'
        assert maintenance.backup(cfg,snapshot)['verified']
        manifest=maintenance.verify(snapshot)
        assert any(item['path']==relative for item in manifest['files'])
        restored=tmp_path/'retention-restored'
        maintenance.restore(snapshot,restored)
        assert (restored/relative).read_bytes()==original
        assert db.setting(restored/'phonoteka.sqlite3','recovery_required')=='true'
        (snapshot/relative).write_bytes(original+b'changed')
        with pytest.raises(ValueError,match='hash'):
            maintenance.verify(snapshot)
        with pytest.raises(ValueError,match='hash'):
            maintenance.restore(snapshot,tmp_path/'damaged-retention-restore')
        assert not (tmp_path/'damaged-retention-restore').exists()


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
