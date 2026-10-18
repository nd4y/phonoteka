import subprocess
import pytest
from mutagen.id3 import ID3,TXXX
from phonoteka import album_loudness,media
from phonoteka.app import create_app
from fastapi.testclient import TestClient
from test_backend import settings,authenticated,job,drain


def fixture(settings,name,volume,duration=3):
    path=settings.work_root/name
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'sine=frequency=440:duration={duration}','-af',f'volume={volume}','-c:a','flac',str(path)],check=True)
    return {'path':path,'sha256':media.sha256_file(path)}


def test_album_joint_gate_not_arithmetic_gain_mean(settings):
    items=[fixture(settings,'loud.flac',1),fixture(settings,'quiet.flac',.01,9)]
    result=album_loudness.analyze(items,settings.roots)
    assert result[0]['tags']['replaygain_album_gain']==result[1]['tags']['replaygain_album_gain']
    gains=[float(c['tags']['replaygain_track_gain'].split()[0]) for c in result]
    album=float(result[0]['tags']['replaygain_album_gain'].split()[0])
    # The quiet track falls below the joint relative gate, despite longer duration.
    assert abs(album-gains[0])<1 and abs(album-sum(gains)/2)>10
    for i,c in zip(items,result):
        assert media.sha256_file(i['path'])==i['sha256']
        changed=media.apply_changes(i['path'],c['tags'],None,i['sha256'],settings.state_dir/i['path'].stem,settings.roots)
        assert media.inspect_audio(i['path'],settings.roots)['tags']['replaygain_album_gain']==[c['tags']['replaygain_album_gain']]
        media.rollback(i['path'],changed['backup_path'],changed['after_sha256'],settings.roots,backup_sha256=i['sha256'],backup_dir=settings.state_dir/('rollback-'+i['path'].stem))
        assert media.sha256_file(i['path'])==i['sha256']


def test_album_cancel_does_not_change_input(settings):
    item=fixture(settings,'cancel.flac',1)
    def cancelled():raise media.MediaError('cancelled','test')
    with pytest.raises(media.MediaError,match='test'):album_loudness.analyze([item],settings.roots,cancelled)
    assert media.sha256_file(item['path'])==item['sha256']


def test_album_job(settings):
    items=[fixture(settings,'one.flac',1),fixture(settings,'two.flac',.5)]
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);job(client,headers,'scan');drain(settings)
        ids=[t['id'] for t in client.get('/api/tracks').json()['items']]
        queued=job(client,headers,'replaygain_album',ids);drain(settings)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        for tid in ids:assert 'replaygain_album_gain' in client.get('/api/tracks/'+tid).json()['analysis']['candidates'][0]['tags']
    assert all(media.sha256_file(i['path'])==i['sha256'] for i in items)


@pytest.mark.parametrize('ext,codec',[('mp3','libmp3lame'),('m4a','aac')])
def test_existing_uppercase_gain_updated_without_duplicate(settings,ext,codec):
    path=settings.work_root/('case.'+ext)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=1','-c:a',codec,str(path)],check=True)
    if ext=='mp3':
        tags=ID3(path);tags.add(TXXX(encoding=3,desc='REPLAYGAIN_TRACK_GAIN',text=['-1.00 dB']));tags.save(path)
    else:
        from mutagen.mp4 import MP4
        tags=MP4(path);tags['----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN']=[b'-1.00 dB'];tags.save()
    assert media.inspect_audio(path,settings.roots)['tags']['replaygain_track_gain']==['-1.00 dB']
    media.apply_changes(path,{'replaygain_track_gain':'-2.00 dB'},None,media.sha256_file(path),settings.state_dir/'backup',settings.roots)
    if ext=='mp3':assert len([x for x in ID3(path).getall('TXXX') if x.desc.lower()=='replaygain_track_gain'])==1
    else:assert len([k for k in MP4(path) if k.lower()=='----:com.apple.itunes:replaygain_track_gain'])==1
    assert media.inspect_audio(path,settings.roots)['tags']['replaygain_track_gain']==['-2.00 dB']
