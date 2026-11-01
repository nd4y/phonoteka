"""Real generated media: exact native slot choice, no unselected data loss."""
import base64
import json
from pathlib import Path
import subprocess
import uuid

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, SYLT, USLT
from mutagen.mp4 import MP4, MP4Cover

from phonoteka import db, media, media_slots, metadata_slots
from test_backend import authenticated, client, settings
from test_media import LINUX, TOOLS, backup_dir, cover_bytes, roots


def generated(directory, extension):
    path = directory / ('slots.' + extension)
    codec = {'mp3': 'libmp3lame', 'flac': 'flac', 'm4a': 'aac', 'ogg': 'libvorbis', 'opus': 'libopus', 'wav': 'pcm_s16le', 'aiff': 'pcm_s16be'}[extension]
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:a', codec, '-y', str(path)], check=True)
    audio = media._load_audio(path)
    if audio.tags is None: audio.add_tags()
    images = [cover_bytes(color) for color in ('red', 'green', 'blue')]
    if extension in {'mp3', 'wav', 'aiff'}:
        for index, raw in enumerate(images):
            audio.tags.add(APIC(encoding=3, mime='image/png', type=3 if index < 2 else 4, desc='picture-' + str(index), data=raw))
        for language, text in [('eng', 'First'), ('rus', 'Второй')]:
            audio.tags.add(SYLT(encoding=3, lang=language, desc=language, format=2, type=1, text=[(text, 100)]))
            audio.tags.add(USLT(encoding=3, lang=language, desc='plain-' + language, text='Separate ordinary ' + text))
        audio.save(v2_version=4)
    else:
        if isinstance(audio, MP4):
            audio['covr'] = [MP4Cover(raw, imageformat=MP4Cover.FORMAT_PNG) for raw in images]
            audio['©lyr'] = ['[00:00.100]First', '[00:00.200]Второй']
        else:
            pictures = []
            for index, raw in enumerate(images):
                picture = Picture(); picture.type = 3 if index < 2 else 4; picture.mime = 'image/png'; picture.desc = 'picture-' + str(index); picture.width = picture.height = 30; picture.depth = 24; picture.data = raw
                pictures.append(picture)
            if isinstance(audio, FLAC):
                for picture in pictures: audio.add_picture(picture)
            else: audio['metadata_block_picture'] = [base64.b64encode(picture.write()).decode() for picture in pictures]
            audio['lyrics'] = ['[00:00.100]First', '[00:00.200]Второй']
            audio['unsyncedlyrics'] = ['Preserved ordinary lyrics']
        audio.save()
    return path


def selected(audio):
    inventory = metadata_slots.inventory(audio)
    return {'inventory_sha256': inventory['inventory_sha256'], **{
        kind + '_slot_id': [item for item in inventory['slots'] if item['kind'] == kind and item['editable']][1]['id']
        for kind in ('cover', 'lyrics')}}


@LINUX
@TOOLS
@pytest.mark.parametrize('extension', ['mp3', 'flac', 'm4a', 'ogg', 'opus', 'wav', 'aiff'])
def test_explicit_second_cover_and_lyric_keep_every_other_native_slot(roots, backup_dir, extension):
    path = generated(roots['work'], extension)
    before_sha = media.sha256_file(path); before_identity = media.audio_identity(path)
    before = media._load_audio(path); choice = selected(before)
    targets = metadata_slots.targets(before, choice, lyrics=True, cover=True)
    preserved = metadata_slots.preservation(before, targets)
    before_inventory = media.inspect_audio(path, roots)['slot_inventory']
    assert len([slot for slot in before_inventory['slots'] if slot['kind'] == 'cover']) == 3
    if extension in {'mp3', 'wav', 'aiff'}:
        assert len([slot for slot in before_inventory['slots'] if slot['native_type'] == 'USLT']) == 2
    result = media.apply_changes(path, {'title': ['Owner']}, '[00:00.500]Selected words', before_sha, backup_dir, roots,
                                 cover_bytes('yellow'), slot_selection=choice)
    after = media._load_audio(path)
    assert media.audio_identity(path) == before_identity
    assert metadata_slots.preservation(after, targets) == preserved
    metadata_slots.verify(after, targets, '[00:00.500]Selected words', cover_bytes('yellow'))
    assert result['metadata_slot_targets'] == {kind: target['native_identity'] for kind, target in targets.items()}


@LINUX
@TOOLS
def test_ambiguous_or_stale_slot_choice_aborts_before_journal(roots, backup_dir):
    path = generated(roots['work'], 'flac'); sha = media.sha256_file(path)
    with pytest.raises(media.MediaError) as ambiguous:
        media.apply_changes(path, {}, '[00:00.500]New words', sha, backup_dir, roots, cover_bytes('yellow'))
    assert ambiguous.value.code == 'lyrics_conflict'
    choice = selected(media._load_audio(path)); choice['inventory_sha256'] = '0' * 64
    with pytest.raises(media.MediaError) as stale:
        media.apply_changes(path, {}, '[00:00.500]New words', sha, backup_dir, roots, slot_selection=choice)
    assert stale.value.code == 'slot_conflict'
    assert media.sha256_file(path) == sha and not list(backup_dir.rglob('*.json'))


@TOOLS
def test_slot_api_is_private_and_proposal_requires_current_inventory(client, settings):
    path = generated(settings.work_root, 'flac'); metadata = media.inspect_audio(path, settings.roots); identity = str(uuid.uuid4())
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)', (identity, path.name, metadata['sha256'], db.encode(metadata), db.utcnow(), db.utcnow()))
    url = '/api/tracks/' + identity
    assert client.get(url + '/slots').status_code == 401
    headers = authenticated(client, settings)
    inventory = client.get(url + '/slots').json()
    assert inventory['file_revision'] == metadata['sha256'] and len(inventory['slots']) == 6
    request_headers = {**headers, 'If-Match': metadata['sha256']}
    assert client.post(url + '/proposal', headers=request_headers, json={'lrc': '[00:00.500]Owner words'}).status_code == 409
    choice = selected(media._load_audio(path)); stale = {**choice, 'inventory_sha256': '0' * 64}
    assert client.post(url + '/proposal', headers=request_headers, json={'lrc': '[00:00.500]Owner words', 'slot_selection': stale}).status_code == 412
    response = client.post(url + '/proposal', headers=request_headers, json={'lrc': '[00:00.500]Owner words', 'slot_selection': choice})
    assert response.status_code == 201, response.text
    assert response.json()['slot_selection'] == choice
    cover = next(slot for slot in inventory['slots'] if slot['kind'] == 'cover')
    image = client.get(url + '/slots/' + cover['id'] + '/cover')
    assert image.status_code == 200 and image.headers['content-type'] == 'image/png'
    assert image.headers['cache-control'] == 'private, no-store'


def test_selected_document_preserves_known_rights_of_retained_first_slot():
    old_source = {'usage': {'origin': 'provider_acquired', 'provider': 'lrclib', 'allow_export': False}}
    old = {'lrc': 'First words', 'usage_provenance': {'lrc': old_source}, 'slot_inventory': {'slots': [
        {'kind': 'lyrics', 'editable': True, 'native_identity': 'lyrics:0'}, {'kind': 'lyrics', 'editable': True, 'native_identity': 'lyrics:1'}]}}
    metadata = {'usage_provenance': {'lrc': {'origin': 'owner_supplied'}}}
    media_slots.committed_metadata(old, metadata, {'lrc': '[00:00.500]Second words'}, {'metadata_slot_targets': {'lyrics': 'lyrics:1'}})
    assert metadata['preferred_lyrics_slot'] == 'lyrics:1'
    assert metadata['lrc'] == '[00:00.500]Second words'
    assert old_source in metadata['usage_provenance'].values()


def test_readonly_uslt_provenance_survives_creating_a_separate_sylt():
    previous = {'usage': {'origin': 'provider_acquired', 'provider': 'lrclib', 'allow_export': False}}
    old = {'lyrics': 'Existing ordinary words', 'usage_provenance': {'lrc': previous}, 'slot_inventory': {'slots': [
        {'kind': 'lyrics', 'editable': False, 'native_type': 'USLT', 'native_identity': 'USLT:original:rus'}]}}
    metadata = {'usage_provenance': {'lrc': {'origin': 'owner_supplied'}}}
    media_slots.committed_metadata(old, metadata, {'lrc': '[00:00.500]Owner timed words'}, {'metadata_slot_targets': {'lyrics': 'SYLT:Phonoteka:und'}})
    assert previous in metadata['usage_provenance'].values()


@LINUX
@TOOLS
@pytest.mark.parametrize('extension', ['flac', 'm4a', 'ogg'])
def test_removing_first_lyric_value_preserves_shifted_second_value(roots, backup_dir, extension):
    path = generated(roots['work'], extension); audio = media._load_audio(path)
    inventory = metadata_slots.inventory(audio)
    first = next(item for item in inventory['slots'] if item['kind'] == 'lyrics' and item['editable'])
    choice = {'inventory_sha256': inventory['inventory_sha256'], 'lyrics_slot_id': first['id']}
    media.apply_changes(path, {}, '', media.sha256_file(path), backup_dir, roots, slot_selection=choice)
    remaining = [item['text'] for item in metadata_slots.native(media._load_audio(path)) if item['kind'] == 'lyrics' and item['editable']]
    assert remaining == ['[00:00.200]Второй']


@LINUX
@TOOLS
def test_recovery_retains_durable_native_target_and_rollback_restores_prior_canonical(roots, backup_dir):
    path = generated(roots['work'], 'flac'); before = media.inspect_audio(path, roots)
    choice = selected(media._load_audio(path)); native_before = metadata_slots.targets(media._load_audio(path), choice, lyrics=True)['lyrics']
    before['preferred_lyrics_slot'] = native_before['native_identity']; before['lrc'] = native_before['text']
    snapshot = media_slots.canonical_snapshot(before)
    result = media.apply_changes(path, {}, '[00:00.500]New selected words', before['sha256'], backup_dir / 'apply', roots, slot_selection=choice)
    durable = json.loads(Path(result['journal_path']).read_text())
    assert durable['metadata_slot_targets']['lyrics'] == 'lyrics:1'
    recovered = media.recover(result['journal_path'], roots)
    after = media_slots.committed_metadata(before, media.inspect_audio(path, roots), {'lrc': '[00:00.500]New selected words'}, recovered)
    assert after['preferred_lyrics_slot'] == 'lyrics:1' and after['lrc'] == '[00:00.500]New selected words'
    media.rollback(path, result['backup_path'], media.sha256_file(path), roots, backup_sha256=before['sha256'], backup_dir=backup_dir / 'rollback')
    restored = media_slots.restore_canonical(path, media.inspect_audio(path, roots), snapshot)
    assert restored['lrc'] == '[00:00.200]Второй' and restored['preferred_lyrics_slot'] == 'lyrics:1'
