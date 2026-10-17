import hashlib
import os
from pathlib import Path
import wave

import pytest
from phonoteka import cue
from phonoteka.media import MediaError


def wav(path, seconds=3):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00' * 8000 * seconds)


def sheet(name='album.wav'):
    return f'''PERFORMER "Artist"
TITLE "Album"
FILE "{name}" WAVE
 TRACK 01 AUDIO
  TITLE "First"
  INDEX 01 00:00:00
 TRACK 02 AUDIO
  TITLE "Second"
  INDEX 00 00:00:60
  INDEX 01 00:01:15
'''


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'cp1251'])
def test_encodings_and_frame_semantics(encoding):
    document = cue.parse(sheet().replace('Artist', 'Исполнитель').encode(encoding))
    assert document['performer'] == 'Исполнитель'
    assert document['tracks'][1]['start_seconds'] == 1.2
    assert document['tracks'][1]['indexes']['0']['frames'] == 60


def test_multifile_eac_pregap_does_not_shift_audio():
    raw = b'''FILE "a.wav" WAVE
TRACK 01 AUDIO
INDEX 01 00:00:00
TRACK 02 AUDIO
INDEX 00 00:01:00
FILE "b.wav" WAVE
INDEX 01 00:00:00
PREGAP 00:02:00
POSTGAP 00:01:00
'''
    track = cue.parse(raw)['tracks'][1]
    assert track['file_index'] == 1 and track['start_seconds'] == 0
    assert track['indexes']['0']['file_index'] == 0
    assert track['pregap_frames'] == 150


@pytest.mark.parametrize('raw', [
    'FILE "a.wav" WAVE\nTRACK 01 AUDIO',
    'FILE "a.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:75',
    'FILE "a.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00\nINDEX 01 00:00:00',
    'FILE "a.bin" BINARY\nTRACK 01 MODE1/2352',
    'FILE "a.wav" WAVE\nTRACK 01 AUDIO\nINDEX 00 00:02:00\nINDEX 01 00:01:00',
    'GARBAGE',
])
def test_invalid_cue_rejected(raw):
    with pytest.raises(MediaError):
        cue.parse(raw.encode())


def test_inspect_playback_and_stale_dependencies(tmp_path):
    audio = tmp_path/'album.wav'; wav(audio)
    path = tmp_path/'album.cue'; path.write_text(sheet())
    meta = cue.inspect_cue(path, tmp_path)
    assert meta['cue']['errors'] == []
    assert [t['duration'] for t in meta['cue']['tracks']] == [1.2, 1.8]
    assert cue.playback_path(tmp_path, path.name, meta, 2) == audio
    with pytest.raises(MediaError, match='недоступна'):
        cue.playback_path(tmp_path, path.name, meta, 3)
    stamp = audio.stat().st_mtime_ns
    os.utime(audio, ns=(stamp, stamp+1000000))
    with pytest.raises(MediaError) as exc:
        cue.playback_path(tmp_path, path.name, meta, 1)
    assert exc.value.code == 'cue_stale'
    meta = cue.inspect_cue(path, tmp_path)
    path.write_text(sheet()+'REM changed\n')
    with pytest.raises(MediaError) as exc:
        cue.playback_path(tmp_path, path.name, meta, 1)
    assert exc.value.code == 'cue_stale'


def test_lossless_fallback_is_unique_and_does_not_change_cue(tmp_path):
    audio = tmp_path/'ALBUM.flac'; wav(audio)
    path = tmp_path/'a.cue'; path.write_text(sheet())
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    resolved, warning = cue.resolve_reference(tmp_path, path, 'album.wav')
    assert resolved == audio and warning
    wav(tmp_path/'album.ape')
    with pytest.raises(MediaError) as exc:
        cue.resolve_reference(tmp_path, path, 'album.wav')
    assert exc.value.code == 'cue_file_ambiguous'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('name', ['../a.wav', '/tmp/a.wav', 'C:\\a.wav', 'https://host/a.wav', '..\\a.wav'])
def test_unsafe_paths_rejected(tmp_path, name):
    with pytest.raises(MediaError):
        cue.resolve_reference(tmp_path, tmp_path/'a.cue', name)


def test_symlink_rejected(tmp_path):
    audio = tmp_path/'real.wav'; wav(audio)
    (tmp_path/'link.wav').symlink_to(audio)
    with pytest.raises(MediaError):
        cue.resolve_reference(tmp_path, tmp_path/'a.cue', 'link.wav')


def test_missing_and_invalid_cue_remain_visible(tmp_path):
    path = tmp_path/'a.cue'; path.write_text(sheet())
    meta = cue.inspect_cue(path, tmp_path)
    assert meta['cue']['errors'][0]['code'] == 'cue_file_missing'
    assert all(not t['available'] for t in meta['cue']['tracks'])
    path.write_text('invalid')
    assert cue.inspect_cue(path, tmp_path)['cue']['errors'][0]['code'] == 'invalid_cue'


def test_bounds_and_multiple_files(tmp_path):
    wav(tmp_path/'album.wav', 1)
    path = tmp_path/'a.cue'; path.write_text(sheet())
    assert cue.inspect_cue(path, tmp_path)['cue']['errors'][0]['code'] == 'cue_invalid_bounds'
    wav(tmp_path/'second.wav', 2)
    path.write_text('FILE "album.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00\nFILE "second.wav" WAVE\nTRACK 02 AUDIO\nINDEX 01 00:00:00')
    meta = cue.inspect_cue(path, tmp_path)
    assert meta['cue']['errors'] == []
    assert [t['duration'] for t in meta['cue']['tracks']] == [1, 2]
