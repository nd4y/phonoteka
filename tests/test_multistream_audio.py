"""AUD-01: real containers, with independent observations of a second stream."""
import json
import subprocess

from phonoteka import media
from test_media import roots, TOOLS


def two_streams(path, second_frequency=880):
    subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','sine=frequency=440:sample_rate=8000:duration=1',
                    '-f','lavfi','-i',f'sine=frequency={second_frequency}:sample_rate=8000:duration=1',
                    '-map','0:a','-map','1:a','-c:a','flac',str(path)],check=True)
    return path


def pcm_hash(path, ordinal):
    return subprocess.check_output(['ffmpeg','-v','error','-nostdin','-i',str(path),'-map',f'0:a:{ordinal}',
                                    '-f','md5','-']).strip()


@TOOLS
def test_all_declared_streams_are_decoded_and_second_stream_changes_identity(roots):
    original=two_streams(roots['work']/'two.mka')
    changed=two_streams(roots['work']/'changed-second.mka',1760)
    assert pcm_hash(original,0)==pcm_hash(changed,0)
    assert pcm_hash(original,1)!=pcm_hash(changed,1)
    before=original.read_bytes()
    result=media.audit_audio(original,roots)
    assert result['state']=='passed' and result['audio_stream_count']==2
    assert len(result['audio_streams'])==2
    assert all(stream['channels']==1 and stream['sample_rate']==8000 for stream in result['audio_streams'])
    assert result['profile']=='full_decode_all_audio_streams'
    assert result['audio_identity_scheme']=='encoded_audio_packets_v2_all_streams'
    assert result['audio_identity']!=media.audio_identity(changed)
    assert original.read_bytes()==before
    assert result['signal']['scope']=='first_audio_stream_only'


@TOOLS
def test_corrupted_second_stream_never_receives_successful_audit(roots):
    path=two_streams(roots['work']/'bad-second.mka')
    first_before=pcm_hash(path,0)
    packet_doc=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','a:1',
        '-show_packets','-show_entries','packet=pos,size','-of','json',str(path)]))
    # Corrupt FLAC's first frame sync in the second stream while retaining the
    # container headers and every packet in the first stream.
    packet=packet_doc['packets'][0];position=int(packet['pos'])
    damaged=bytearray(path.read_bytes())
    # Matroska packet positions may precede the Block header. Locate FLAC's
    # frame sync within this packet instead of damaging the container itself.
    sync=next((index for index in range(position,min(len(damaged)-1,position+int(packet['size'])+32))
               if damaged[index]==255 and damaged[index+1]&254==248),None)
    assert sync is not None,'Generated second stream has no FLAC frame sync'
    damaged[sync]^=255;path.write_bytes(damaged)
    second=subprocess.run(['ffmpeg','-v','error','-nostdin','-xerror','-i',str(path),'-map','0:a:1','-f','null','-'],capture_output=True)
    assert pcm_hash(path,0)==first_before and second.returncode!=0
    before=path.read_bytes()
    result=media.audit_audio(path,roots)
    assert result['state']=='failed' and result['error']=='invalid_audio'
    assert path.read_bytes()==before
