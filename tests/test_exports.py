import json
from fastapi.testclient import TestClient
from phonoteka import db,exports,media
from phonoteka.app import create_app
from test_backend import settings,authenticated,seed,job,drain
import pytest


def published(settings,relative='Артист/#песня.mp3'):
    tid,sha=seed(settings,relative)
    target=settings.publication_root/'Ready'/relative;target.parent.mkdir(parents=True);target.write_bytes((settings.work_root/relative).read_bytes())
    row=db.fetchone(settings.db_path,'SELECT metadata FROM tracks WHERE id=?',(tid,));meta=json.loads(row['metadata'])
    meta['publication']={'relative_path':relative,'sha256':sha};meta['duration']=3.2
    with db.transaction(settings.db_path) as conn:conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),tid))
    return tid,sha,target


def test_verified_export_portable_playlist_and_receipts(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);tid,sha,target=published(settings)
        queued=job(client,headers,'export_ready');drain(settings)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        doc=client.get('/api/exports/latest').json()['export'];identity=doc['id']
        playlist=client.get(f'/api/exports/{identity}/playlist.m3u8')
        assert playlist.status_code==200 and './Артист/#песня.mp3' in playlist.text
        assert doc['verified_on']=='server_ready' and not doc['delivery_verified']
        data={'export_id':identity,'device':'test-phone','checksums':{'Артист/#песня.mp3':'0'*64}}
        assert client.post('/api/deliveries',json=data,headers=headers).status_code==409
        data['checksums']={'Артист/#песня.mp3':sha}
        saved=client.post('/api/deliveries',json=data,headers=headers)
        assert saved.status_code==201 and saved.json()['state']=='client_hashes_match'
        assert saved.json()['verification_origin']=='owner_report'
        assert not saved.json()['poweramp_checked']
        with db.transaction(settings.db_path) as conn:conn.execute('UPDATE tracks SET sha256=? WHERE id=?',('f'*64,tid))
        assert client.get('/api/exports/latest').json()['stale_files']==1


def test_changed_ready_and_unsafe_playlist_path_refuse_snapshot(settings):
    db.initialize(settings.db_path)
    tid,sha,target=published(settings)
    target.write_bytes(b'changed externally')
    with pytest.raises(media.MediaError):exports.generate(settings)
    assert not db.setting(settings.db_path,'latest_export')


def test_no_export_or_device_claim_without_evidence(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings)
        assert client.get('/api/exports/latest').json()['export'] is None
        assert client.get('/api/exports/00000000-0000-0000-0000-000000000000/manifest.json').status_code==404
        tid,sha,target=published(settings);identity=exports.generate(settings)['export_id']
        response=client.post('/api/deliveries',json={'export_id':identity,'device':'phone','poweramp_checked':True},headers=headers)
        assert response.status_code==422
