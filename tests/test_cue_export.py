import os
import struct
import wave

import pytest

from phonoteka import cue, cue_export, media
from test_backend import settings, authenticated, job, drain
from phonoteka.app import create_app
from dataclasses import replace
from fastapi.testclient import TestClient


def fixture(settings, lead=False):
    path=settings.work_root/'album.wav'
    pcm=b''.join(struct.pack('<h',(i*53)%30000-15000) for i in range(24000))
    with wave.open(str(path),'wb') as wav:
        wav.setparams((1,2,8000,0,'NONE','not compressed'));wav.writeframes(pcm)
    sheet=settings.work_root/'album.cue'
    sheet.write_text('PERFORMER "Artist"\nTITLE "Album"\nFILE "album.wav" WAVE\nTRACK 01 AUDIO\nTITLE "First"\nINDEX 01 '+('00:00:15' if lead else '00:00:00')+'\nTRACK 02 AUDIO\nTITLE "Second"\nINDEX 00 00:00:60\nINDEX 01 00:01:15\n',encoding='utf-8')
    return path,sheet,pcm


def test_exact_samples_gaps_lead_in_and_idempotence(settings):
    path,sheet,pcm=fixture(settings,lead=True);digest=media.sha256_file(path)
    meta=cue.inspect_cue(sheet,settings.work_root)
    result=cue_export.export(sheet,meta,settings)
    assert len(result['files'])==3 and result['files'][0]['lead_in']
    assert result['files'][1]['start_sample']==1600 and result['files'][1]['end_sample']==9600
    decoded=b''.join(media._process(settings.work_root/x['relative_path'],'ffmpeg',['-map','0:a:0','-f','s16le','-']).stdout for x in result['files'])
    assert decoded==pcm and media.sha256_file(path)==digest
    assert cue_export.export(sheet,meta,settings)['reused'] is True
    target=settings.work_root/result['files'][1]['relative_path']
    assert media.inspect_audio(target,settings.roots)['tags']['title']==['First']
    with target.open('ab') as f:f.write(b'external')
    with pytest.raises(media.MediaError):cue_export.export(sheet,meta,settings)


def test_cancel_and_stale_source_never_publish_partial_album(settings):
    path,sheet,_=fixture(settings);meta=cue.inspect_cue(sheet,settings.work_root)
    def cancelled():raise media.MediaError('cancelled','stopped')
    with pytest.raises(media.MediaError,match='stopped'):cue_export.export(sheet,meta,settings,cancelled)
    assert not list((settings.work_root/'.phonoteka-derived').iterdir())
    os.utime(path,ns=(path.stat().st_atime_ns,path.stat().st_mtime_ns+100000))
    with pytest.raises(media.MediaError,match='изменился'):cue_export.export(sheet,meta,settings)


def test_job_indexes_derivatives_and_keeps_album_guard(settings):
    cfg=replace(settings,read_only=False,storage_verified=True)
    fixture(cfg)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        tracks=client.get('/api/tracks').json()['items']
        sheet=next(t for t in tracks if t['format']=='cue');image=next(t for t in tracks if t['format']=='wav')
        exported=job(client,headers,'extract_cue',[sheet['id']]);drain(cfg)
        result=client.get('/api/jobs/'+exported['id']).json()
        assert result['state']=='succeeded',result
        tracks=client.get('/api/tracks').json()['items']
        assert len(tracks)==4
        assert client.get('/api/tracks/'+image['id']).json()['cue_album_image'] is True
        assert len([t for t in tracks if t['relative_path'].startswith('.phonoteka-derived/')])==2
        repeated=job(client,headers,'extract_cue',[sheet['id']]);drain(cfg)
        assert client.get('/api/jobs/'+repeated['id']).json()['state']=='succeeded'
        assert client.get('/api/tracks').json()['total']==4


def test_float_lossy_source_uses_lossless_pcm_wav(settings):
    import subprocess
    source=settings.work_root/'album.mp3'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','libmp3lame',str(source)],check=True)
    sheet=settings.work_root/'album.cue'
    sheet.write_text('FILE "album.mp3" MP3\nTRACK 01 AUDIO\nINDEX 01 00:00:00\nTRACK 02 AUDIO\nINDEX 01 00:01:00\n',encoding='utf-8')
    before=media.sha256_file(source)
    result=cue_export.export(sheet,cue.inspect_cue(sheet,settings.work_root),settings)
    assert all(x['profile']['codec']=='pcm_f32le' for x in result['files'])
    expected=media._process(source,'ffmpeg',['-map','0:a:0','-f','f32le','-']).stdout
    actual=b''.join(media._process(settings.work_root/x['relative_path'],'ffmpeg',['-map','0:a:0','-f','f32le','-']).stdout for x in result['files'])
    assert actual==expected and media.sha256_file(source)==before
