import json
import subprocess
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from phonoteka import media,derivatives
from phonoteka.app import create_app
from test_backend import settings,authenticated,job,drain


@pytest.mark.parametrize('ext,codec',[('aac','aac'),('webm','libopus'),('wv','wavpack')])
def test_writable_derivative_preserves_decoded_pcm_and_original(settings,ext,codec):
    path=settings.work_root/('source.'+ext)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2','-c:a',codec,str(path)],check=True)
    before=media.sha256_file(path)
    try:meta=media.inspect_audio(path,settings.roots)
    except media.MediaError as e:meta=media.fallback_inventory(path,settings.roots,e)
    result=derivatives.export(path,meta,settings)
    target=settings.work_root/result['files'][0]['relative_path']
    actual=media.inspect_audio(target,settings.roots)
    assert actual['write_supported'] and media.sha256_file(path)==before
    assert derivatives.export(path,meta,settings)['reused']
    media.apply_changes(target,{'title':'Tagged'},None,actual['sha256'],settings.state_dir/'backup',settings.roots)
    with pytest.raises(media.MediaError,match='changed|revision|SHA|match'):derivatives.export(path,meta,settings)


def test_normalize_job_catalogs_lineage(settings):
    cfg=replace(settings,read_only=False,storage_verified=True)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2','-c:a','aac',str(cfg.work_root/'source.aac')],check=True)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        tid=client.get('/api/tracks').json()['items'][0]['id'];queued=job(client,headers,'normalize_audio',[tid]);drain(cfg)
        detail=client.get('/api/jobs/'+queued['id']).json();assert detail['state']=='succeeded',detail
        tracks=client.get('/api/tracks').json()['items'];assert len(tracks)==2
        derived=next(t for t in tracks if t['id']!=tid)
        assert derived['derived_from']['parent_track_id']==tid and derived['can_write']


def test_decode_recovery_preserves_decodable_pcm_and_original_via_api(settings):
    cfg=replace(settings,read_only=False,storage_verified=True)
    path=cfg.work_root/'damaged.mp3'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2','-c:a','libmp3lame','-b:a','128k','-write_xing','0',str(path)],check=True)
    # Corrupt the side information inside a real frame. Trailing junk is
    # ignored by newer FFmpeg demuxers and does not guarantee a decode failure.
    packets=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','a:0',
        '-show_packets','-show_entries','packet=pos','-of','json',str(path)]))['packets']
    offset=int(packets[len(packets)//2]['pos'])
    damaged=bytearray(path.read_bytes())
    assert damaged[offset]==255 and damaged[offset+1]&254==250
    damaged[offset+4:offset+21]=b'\xff'*17
    path.write_bytes(damaged)
    original=media.sha256_file(path)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        tid=client.get('/api/tracks').json()['items'][0]['id']
        job(client,headers,'audit',[tid]);drain(cfg)
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['audit']['state']=='failed' and detail['audit']['failure_stage']=='decode',detail['audit']
        assert detail['can_recover']
        queued=job(client,headers,'normalize_audio',[tid]);drain(cfg)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        child=next(row for row in client.get('/api/tracks').json()['items'] if row['id']!=tid)
        assert child['derived_from']['recovery']['human_acceptance'] is False
        target=cfg.work_root/child['relative_path']
        def pcm_hash(source):return media._process(source,'ffmpeg',['-nostdin','-map','0:a:0','-c:a','pcm_f32le','-f','hash','-hash','sha256','-']).stdout.strip()
        assert pcm_hash(target)==pcm_hash(path)
        assert media.audit_audio(target,cfg.roots)['state']=='passed'
        assert media.sha256_file(path)==original


@pytest.mark.parametrize('change',[
    {'audit':{'sha256':'current','state':'passed','failure_stage':'decode'}},
    {'audit':{'sha256':'old','state':'failed','failure_stage':'decode'}},
    {'audit':{'sha256':'current','state':'failed','failure_stage':'header'}},
    {'audio_stream_count':2},{'input_error':{'code':'invalid_audio'}},{'cue_album_image':True},
])
def test_recovery_requires_a_current_single_stream_decode_failure(change):
    meta={'format':'mp3','write_supported':True,'sha256':'current',
          'audit':{'sha256':'current','state':'failed','failure_stage':'decode'}}
    assert derivatives.recovery_eligible(meta)
    assert not derivatives.recovery_eligible({**meta,**change})
