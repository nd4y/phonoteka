"""Regression from real-file failures, using generated audio and literal frames."""
import shutil
import subprocess
import sys

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
