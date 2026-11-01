"""AUD-01 integration: no fingerprint work precedes a current full audit."""
from dataclasses import replace
import json
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from phonoteka import db, media, recognition
from phonoteka.app import create_app
from test_backend import settings, authenticated, job, drain
from test_multistream_audio import two_streams, pcm_hash


def generated(cfg):
    path=cfg.work_root/'recognition.flac'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','sine=frequency=440:duration=4',
                    '-metadata','title=Original','-c:a','flac',str(path)],check=True)
    shutil.copy2(path,cfg.source_root/path.name)
    return path


def matched():
    return {'id':'shazam:fixture','provider':'shazam','kind':'metadata','tags':{'title':'Known','artist':'Fixture'},
            'identification':'audio_fingerprint','match':{'state':'fingerprint_match','reasons':[]}}


@pytest.mark.parametrize('old_profile',[False,True])
def test_fingerprint_requires_current_server_audit_then_reuses_it_without_packet_probe(settings,monkeypatch,old_profile):
    cfg=replace(settings,external_fingerprints=True,external_metadata=False);path=generated(cfg)
    audit_calls=[];lookups=[];actual_audit=media.audit_audio
    def audit(*args):
        audit_calls.append(args[0]);return actual_audit(*args)
    async def identify(*args,**kwargs):
        row=db.fetchone(cfg.db_path,'SELECT metadata,sha256 FROM tracks WHERE relative_path=? AND is_current=1',(path.name,))
        data=json.loads(row['metadata'])
        assert data['audit']['state']=='passed' and data['audit']['sha256']==row['sha256']
        assert data['audit']['profile']=='full_decode_all_audio_streams'
        assert data['hash_verified_at']==data['audit']['hash_verified_at']
        lookups.append(data['audit']['audio_identity']);return matched()
    monkeypatch.setattr(media,'audit_audio',audit);monkeypatch.setattr(recognition,'recognize',identify)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        track=client.get('/api/tracks').json()['items'][0]
        if old_profile:
            with db.transaction(cfg.db_path) as conn:
                row=conn.execute('SELECT metadata FROM tracks WHERE id=?',(track['id'],)).fetchone()
                data=json.loads(row['metadata'])
                data['audit']={'state':'passed','sha256':track['sha256'],'audio_identity':'a'*64,'profile':'legacy_first_stream'}
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(data),track['id']))
        # API proposal fields cannot manufacture a server audit record.
        forged=client.post('/api/tracks/'+track['id']+'/proposal',headers={**headers,'If-Match':track['sha256']},
                           json={'audit':{'state':'passed','profile':'full_decode_all_audio_streams'}})
        assert forged.status_code==422
        first=job(client,headers,'recognize',[track['id']]);drain(cfg)
        assert client.get('/api/jobs/'+first['id']).json()['state']=='succeeded'
        assert len(audit_calls)==1 and len(lookups)==1
        stored=client.get('/api/tracks/'+track['id']).json()['audit']
        assert stored['checked_at'] and stored['audio_identity']==lookups[0]
        def repeated(*args,**kwargs):raise AssertionError('Current full audit must be reused')
        monkeypatch.setattr(media,'audit_audio',repeated);monkeypatch.setattr(media,'audio_identity',repeated)
        second=job(client,headers,'recognize',[track['id']]);drain(cfg)
        assert client.get('/api/jobs/'+second['id']).json()['state']=='succeeded'
        assert lookups==[stored['audio_identity'],stored['audio_identity']]


def test_corrupt_secondary_stream_stops_before_sample_reservation_and_network(settings,monkeypatch):
    cfg=replace(settings,external_fingerprints=True,external_metadata=False)
    path=two_streams(cfg.work_root/'bad-secondary.mka')
    shutil.copy2(path,cfg.source_root/path.name);source=(cfg.source_root/path.name).read_bytes()
    first=pcm_hash(path,0)
    packets=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','a:1','-show_packets',
        '-show_entries','packet=pos,size','-of','json',str(path)]))['packets']
    packet=packets[0];start=int(packet['pos']);data=bytearray(path.read_bytes())
    sync=next(index for index in range(start,min(len(data)-1,start+int(packet['size'])+32)) if data[index]==255 and data[index+1]&254==248)
    data[sync]^=255;path.write_bytes(data)
    assert pcm_hash(path,0)==first
    calls=[]
    def forbidden(*args,**kwargs):calls.append('unexpected');raise AssertionError('Audit must precede fingerprint sample/lookup')
    monkeypatch.setattr(recognition,'sample',forbidden);monkeypatch.setattr(recognition,'recognize',forbidden)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        track=client.get('/api/tracks').json()['items'][0]
        queued=job(client,headers,'recognize',[track['id']]);drain(cfg)
        result=client.get('/api/jobs/'+queued['id']).json()
        assert result['state']=='failed' and result['results'][0]['code']=='audit_failed',result
        row=db.fetchone(cfg.db_path,'SELECT metadata FROM tracks WHERE id=?',(track['id'],))
        audit=json.loads(row['metadata'])['audit']
        assert audit['state']=='failed' and audit['sha256']==track['sha256'] and audit['checked_at']
        assert not calls and db.fetchone(cfg.db_path,'SELECT COUNT(*) n FROM provider_calls')['n']==0
        assert not db.fetchone(cfg.db_path,"SELECT 1 FROM settings WHERE key LIKE 'recognition_usage:%'")
        assert path.read_bytes()==data and (cfg.source_root/path.name).read_bytes()==source
