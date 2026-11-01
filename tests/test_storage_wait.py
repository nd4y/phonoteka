import contextlib
import errno
import shutil
from dataclasses import replace
from fastapi.testclient import TestClient
from phonoteka import db, media
from phonoteka.app import create_app
from test_backend import settings, synthetic, authenticated, job, drain


def proposals(client, headers, tracks):
    for track in tracks:
        response=client.post('/api/tracks/'+track['id']+'/proposal',json={'tags':{'title':'Accepted '+track['relative_path']}},headers={**headers,'If-Match':track['sha256']})
        assert response.status_code==201,response.text


def test_preflight_space_wait_is_durable_and_resumes_without_replaying_completed(settings,monkeypatch):
    settings=replace(settings,read_only=False,storage_verified=True,file_workers=1)
    first=synthetic(settings);second=settings.work_root/'second.flac';shutil.copy2(first,second)
    before={p.name:media.sha256_file(p) for p in (first,second)}
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        tracks=client.get('/api/tracks').json()['items'];proposals(client,headers,tracks)
        original=media._reserve_filesystem_space;calls=0
        @contextlib.contextmanager
        def reserve(requests):
            nonlocal calls
            calls+=1
            if calls==2:raise media.MediaError('insufficient_space','Synthetic reservation pressure',retryable=True)
            with original(requests):yield
        monkeypatch.setattr(media,'_reserve_filesystem_space',reserve)
        queued=job(client,headers,'apply',[x['id'] for x in tracks]);drain(settings)
        state=client.get('/api/jobs/'+queued['id']).json()
        assert state['state']=='queued' and state['stage']=='waiting_storage' and state['eta_reason']=='waiting_storage'
        assert state['completed']==1 and state['failed']==0 and state['retry_at']
        assert sum(media.sha256_file(p)!=before[p.name] for p in (first,second))==1
        assert client.post('/api/jobs/'+queued['id']+'/retry-storage').status_code==403
        monkeypatch.setattr(media,'_reserve_filesystem_space',original)
        assert client.post('/api/jobs/'+queued['id']+'/retry-storage',headers=headers).status_code==200
        drain(settings)
        assert client.get('/api/jobs/'+queued['id']).json()['completed']==2
        assert len(db.fetchall(settings.db_path,"SELECT id FROM history WHERE kind='apply'"))==2


def test_space_error_after_journal_never_becomes_an_automatic_retry(settings,monkeypatch):
    settings=replace(settings,read_only=False,storage_verified=True)
    path=synthetic(settings);source_sha=media.sha256_file(settings.source_root/path.name)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        tracks=client.get('/api/tracks').json()['items'];proposals(client,headers,tracks)
        original=media._replace
        def replace_then_fail(source,target,**kwargs):
            original(source,target,**kwargs)
            if target==path:raise OSError(errno.ENOSPC,'Synthetic pressure after rename')
        monkeypatch.setattr(media,'_replace',replace_then_fail)
        queued=job(client,headers,'apply',[tracks[0]['id']]);drain(settings)
        state=client.get('/api/jobs/'+queued['id']).json()
        assert state['state']=='interrupted' and state['retry_at'] is None
        assert client.post('/api/jobs/'+queued['id']+'/retry-storage',headers=headers).status_code==409
        assert media.sha256_file(settings.source_root/path.name)==source_sha
        assert list((settings.state_dir/'backups').rglob('*.json'))
        monkeypatch.setattr(media,'_replace',original);drain(settings)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        assert len(db.fetchall(settings.db_path,"SELECT id FROM history WHERE kind='apply'"))==1
