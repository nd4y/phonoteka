"""Regression from real-file failures, using generated audio and literal frames."""
import shutil
import subprocess
import sys
import hashlib

import pytest
from mutagen.id3 import ID3, TIT2, TXXX

from phonoteka import media


def size_bytes(size):
    return bytes((size >> shift) & 127 for shift in (21, 14, 7, 0))


def generated_with_empty_frames(tmp_path, version):
    root = tmp_path / 'work'
    root.mkdir()
    path = root / 'empty-frames.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=1', '-c:a', 'libmp3lame', str(path)], check=True)
    tag = ID3(path)
    tag.add(TIT2(encoding=1 if version == 3 else 3, text=['Original']))
    tag.add(TXXX(encoding=1 if version == 3 else 3, desc='PRIVATE', text=['keep']))
    tag.save(path, v2_version=version)
    original = path.read_bytes()
    old_size = sum(value << shift for value, shift in zip(original[6:10], (21, 14, 7, 0)))
    # Literal native frames, independent of the implementation's serializer.
    frames = b''
    for name, body in ((b'COMM', b'\x01eng\xff\xfe\x00\x00\xff\xfe\x00\x00'),
                       (b'TDAT', b'\x00\x00')):
        size = size_bytes(len(body)) if version == 4 else len(body).to_bytes(4, 'big')
        frames += name + size + b'\x00\x00' + body
    path.write_bytes(original[:6] + size_bytes(old_size + len(frames)) + frames + original[10:])
    roots = {'work': root, 'source': tmp_path / 'original', 'publication': tmp_path / 'publication'}
    for directory in roots.values():
        directory.mkdir(exist_ok=True)
    return path, roots


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'), reason='Linux and FFmpeg required')
@pytest.mark.parametrize('version', [3, 4])
def test_tag_write_preserves_unselected_empty_comment_and_date(tmp_path, version):
    path, roots = generated_with_empty_frames(tmp_path, version)
    before = ID3(path, translate=False)
    assert len(before.getall('COMM')) == len(before.getall('TDAT')) == 1
    signatures = {key: repr(frame) for key, frame in before.items() if key != 'TIT2'}
    audio_before = media.audio_identity(path)
    result = media.apply_changes(path, {'title': 'New'}, None, media.sha256_file(path), tmp_path / 'backup', roots)
    after = ID3(path, translate=False)
    assert after.version[1] == version
    assert after['TIT2'].text == ['New']
    assert {key: repr(frame) for key, frame in after.items() if key != 'TIT2'} == signatures
    assert media.audio_identity(path) == audio_before == result['audio_identity']
    assert result['state'] == 'committed'


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'), reason='Linux and FFmpeg required')
def test_removal_of_an_empty_foreign_frame_still_blocks_replacement(tmp_path, monkeypatch):
    path, roots = generated_with_empty_frames(tmp_path, 3)
    before = path.read_bytes()
    original_edit = media._edit
    def corrupt(staged, tags, lrc, cover):
        original_edit(staged, tags, lrc, cover)
        tag = ID3(staged, translate=False)
        tag.delall('COMM')
        tag.save(staged, v2_version=3)
    monkeypatch.setattr(media, '_edit', corrupt)
    with pytest.raises(media.MediaError) as caught:
        media.apply_changes(path, {'title': 'New'}, None, media.sha256_file(path), tmp_path / 'backup', roots)
    assert caught.value.code == 'metadata_changed'
    assert path.read_bytes() == before


def legacy_tail():
    return (b'TAG'+b'Legacy title'.ljust(30,b'\0')+b'Legacy artist'.ljust(30,b'\0')+
            b'Legacy album'.ljust(30,b'\0')+b'1997'+b'Unselected native comment'.ljust(30,b'\0')+bytes([17]))


def include_legacy_in_demuxed_packets(monkeypatch):
    # Fault-injection contract of the real demuxer on the failed input:
    # the independent packet digest also includes these existing tail bytes.
    # Keep the real encoded-packet check as well, so changing audio still fails.
    real=media.audio_identity
    def identity(path):
        value=real(path)
        tail=path.read_bytes()[-128:]
        return hashlib.sha256(value.encode()+tail).hexdigest()
    monkeypatch.setattr(media,'audio_identity',identity)


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'), reason='Linux and FFmpeg required')
@pytest.mark.parametrize('version',[3,4])
def test_id3v2_edit_preserves_legacy_v1_when_tail_is_in_packet_identity(tmp_path,version,monkeypatch):
    path,roots=generated_with_empty_frames(tmp_path,version)
    tail=legacy_tail();assert len(tail)==128
    with path.open('ab') as output:output.write(tail)
    include_legacy_in_demuxed_packets(monkeypatch)
    identity=media.audio_identity(path)
    result=media.apply_changes(path,{'title':'Updated v2 title','artist':'Selected v2 artist'},None,
                               media.sha256_file(path),tmp_path/'backup',roots)
    after=ID3(path,translate=False)
    assert after['TIT2'].text==['Updated v2 title'] and after['TPE1'].text==['Selected v2 artist']
    assert after.version[1]==version and path.read_bytes()[-128:]==tail
    assert media.audio_identity(path)==identity==result['audio_identity']


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'), reason='Linux and FFmpeg required')
def test_tagging_without_v1_does_not_create_legacy_tail(tmp_path):
    path,roots=generated_with_empty_frames(tmp_path,4)
    assert path.read_bytes()[-128:-125]!=b'TAG'
    media.apply_changes(path,{'title':'New'},None,media.sha256_file(path),tmp_path/'backup',roots)
    assert path.read_bytes()[-128:-125]!=b'TAG'


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'), reason='Linux and FFmpeg required')
def test_ordinary_safe_legacy_updates_keep_existing_behavior(tmp_path):
    path,roots=generated_with_empty_frames(tmp_path,4)
    with path.open('ab') as output:output.write(legacy_tail())
    media.apply_changes(path,{'title':'Updated'},None,media.sha256_file(path),tmp_path/'backup',roots)
    assert path.read_bytes()[-128:-95].rstrip(b'\0')==b'TAGUpdated'


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'), reason='Linux and FFmpeg required')
def test_legacy_fallback_never_accepts_a_changed_audio_packet(tmp_path,monkeypatch):
    path,roots=generated_with_empty_frames(tmp_path,4)
    with path.open('ab') as output:output.write(legacy_tail())
    before=path.read_bytes();original_edit=media._edit
    include_legacy_in_demuxed_packets(monkeypatch)
    def corrupt(staged,*args,**kwargs):
        original_edit(staged,*args,**kwargs)
        data=bytearray(staged.read_bytes())
        size=sum(value<<shift for value,shift in zip(data[6:10],(21,14,7,0)))
        data[10+size+2048]^=1
        staged.write_bytes(data)
    monkeypatch.setattr(media,'_edit',corrupt)
    with pytest.raises(media.MediaError) as caught:
        media.apply_changes(path,{'title':'Updated'},None,media.sha256_file(path),tmp_path/'backup',roots)
    assert caught.value.code=='audio_changed' and path.read_bytes()==before
