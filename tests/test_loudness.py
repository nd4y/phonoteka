from dataclasses import replace
import subprocess

import pytest
from fastapi.testclient import TestClient
from phonoteka import loudness,media
from phonoteka.app import create_app
from test_backend import settings, authenticated, job, drain


@pytest.mark.parametrize('ext,codec',[('flac','flac'),('mp3','libmp3lame'),('m4a','aac'),('ogg','libvorbis'),('opus','libopus'),('wav','pcm_s16le'),('aiff','pcm_s16be')])
def test_gain_analysis_tag_roundtrip_and_rollback(settings,ext,codec):
    path=settings.work_root/('audio.'+ext)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a',codec,str(path)],check=True)
    before=media.sha256_file(path);identity=media.audio_identity(path)
    candidate=loudness.analyze(path,settings.roots,before)
    assert media.sha256_file(path)==before
    result=media.apply_changes(path,candidate['tags'],None,before,settings.state_dir/'backup',settings.roots)
    actual=media.inspect_audio(path,settings.roots)
    assert actual['tags']['replaygain_track_gain']==[candidate['tags']['replaygain_track_gain']]
    assert media.audio_identity(path)==identity
    media.rollback(path,result['backup_path'],result['after_sha256'],settings.roots,backup_sha256=before,backup_dir=settings.state_dir/'rollback')
    assert media.sha256_file(path)==before


def test_loudness_job_proposes_without_writing(settings):
    path=settings.work_root/'audio.flac'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=3',str(path)],check=True)
    before=media.sha256_file(path)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        tid=client.get('/api/tracks').json()['items'][0]['id']
        queued=job(client,headers,'replaygain',[tid]);drain(settings)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['analysis']['candidates'][0]['kind']=='loudness'
        assert media.sha256_file(path)==before and not detail['proposal']
