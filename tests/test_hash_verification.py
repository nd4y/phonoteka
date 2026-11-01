"""LIB-03: only actual, stable full reads advance a track's verification date."""
from dataclasses import replace
from datetime import datetime
import json
import os

from fastapi.testclient import TestClient
from mutagen.flac import FLAC
import pytest

from phonoteka import db, media
from phonoteka.app import create_app
from phonoteka.worker import Worker
import phonoteka.worker as worker_module
from test_backend import settings, synthetic, authenticated, job, drain


def test_successful_full_scan_date_is_public_and_failed_read_does_not_advance(settings,monkeypatch):
    synthetic(settings)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        track=client.get('/api/tracks').json()['items'][0]
        assert datetime.fromisoformat(track['hash_verified_at']).utcoffset().total_seconds()==0
        detail=client.get('/api/tracks/'+track['id']).json()
        assert detail['hash_verified_at']==track['hash_verified_at']
        def failed(*args,**kwargs):raise media.MediaError('file_changing','Injected incomplete full read')
        monkeypatch.setattr(media,'inspect_audio',failed)
        worker=Worker(settings)
        try:
            with pytest.raises(media.MediaError):worker.scan_one(track['relative_path'],None)
        finally:worker.pool.shutdown(wait=True)
        unchanged=client.get('/api/tracks/'+track['id']).json()
        assert unchanged['hash_verified_at']==track['hash_verified_at'] and unchanged['sha256']==track['sha256']


def test_same_size_mtime_external_replacement_gets_its_own_full_hash_date(settings,monkeypatch):
    path=synthetic(settings)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        old=client.get('/api/tracks').json()['items'][0];stat=path.stat()
        audio=FLAC(path);audio['title']=['OriginaL'];audio.save()
        assert path.stat().st_size==stat.st_size
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        verified='2030-01-01T00:00:00.000+00:00'
        monkeypatch.setattr(worker_module.db,'utcnow',lambda:verified)
        worker=Worker(settings)
        try:worker.scan_one(path.name,None)
        finally:worker.pool.shutdown(wait=True)
        current=client.get('/api/tracks').json()['items'][0]
        assert current['id']!=old['id'] and current['sha256']!=old['sha256']
        assert current['hash_verified_at']==verified and current['hash_verified_at']!=old['hash_verified_at']


def test_committed_tag_write_and_successful_audit_have_current_full_hash_dates(settings):
    cfg=replace(settings,read_only=False,storage_verified=True);synthetic(cfg)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        track=client.get('/api/tracks').json()['items'][0]
        response=client.post('/api/tracks/'+track['id']+'/proposal',headers={**headers,'If-Match':track['sha256']},json={'tags':{'title':'Verified write'}})
        assert response.status_code==201,response.text
        mutation=job(client,headers,'apply',[track['id']],{'proposal_id':response.json()['id']});drain(cfg)
        assert client.get('/api/jobs/'+mutation['id']).json()['state']=='succeeded'
        after=client.get('/api/tracks/'+track['id']).json()
        assert after['sha256']!=track['sha256'] and after['hash_verified_at']>=track['hash_verified_at']
        audited=job(client,headers,'audit',[track['id']]);drain(cfg)
        assert client.get('/api/jobs/'+audited['id']).json()['state']=='succeeded'
        row=db.fetchone(cfg.db_path,'SELECT metadata FROM tracks WHERE id=?',(track['id'],))
        meta=json.loads(row['metadata'])
        assert meta['hash_verified_at']==meta['audit']['hash_verified_at']
        assert meta['audit']['sha256']==after['sha256']
