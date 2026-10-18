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
