"""Poweramp-compatible LRC companions must not damage other native ID3 slots."""
import pytest
from mutagen.mp3 import MP3
from mutagen.id3 import SYLT, USLT
from phonoteka import media, metadata_slots
from test_media import LINUX, TOOLS, roots, backup_dir, make_audio

LRC='[00:00.000]Первый сигнал\n[00:01.000]Второй сигнал\n[00:02.000]Третий сигнал'
NEW=LRC.replace('сигнал','звук')


@LINUX
@TOOLS
@pytest.mark.parametrize('version',[3,4])
def test_mp3_poweramp_pair_updates_and_clears_as_one_slot_without_touching_other_lyrics(make_audio,roots,backup_dir,version):
    path=make_audio();audio=MP3(path)
    foreign=USLT(encoding=1,lang='rus',desc='Owner ordinary text',text='Сохранить отдельный текст')
    audio.tags.add(foreign);audio.save(v2_version=version)
    before_identity=media.audio_identity(path);foreign_before=repr(MP3(path).tags[foreign.HashKey])
    media.apply_changes(path,{},LRC,media.sha256_file(path),backup_dir,roots)
    audio=MP3(path);assert audio.tags.version==(2,version,0)
    sylt=audio.tags.getall('SYLT')[0];mirror=audio.tags[USLT(lang=sylt.lang,desc=sylt.desc).HashKey]
    assert sylt.format==2 and sylt.type==1 and mirror.text==LRC
    slots=metadata_slots.inventory(audio)['slots'];editable=[x for x in slots if x['kind']=='lyrics' and x['editable']]
    assert len(editable)==1 and editable[0]['compatibility_mirror']=='USLT'
    assert repr(audio.tags[foreign.HashKey])==foreign_before
    media.apply_changes(path,{},NEW,media.sha256_file(path),backup_dir,roots)
    audio=MP3(path);assert len(audio.tags.getall('SYLT'))==1 and len(audio.tags.getall('USLT'))==2
    assert audio.tags[mirror.HashKey].text==NEW
    assert media.inspect_audio(path,roots)['lrc']==NEW
    pair_before=[repr(audio.tags[key]) for key in (sylt.HashKey,mirror.HashKey)]
    media.apply_changes(path,{'genre':'Test'},None,media.sha256_file(path),backup_dir,roots)
    audio=MP3(path);assert [repr(audio.tags[key]) for key in (sylt.HashKey,mirror.HashKey)]==pair_before
    media.apply_changes(path,{},'',media.sha256_file(path),backup_dir,roots)
    audio=MP3(path);assert not audio.tags.getall('SYLT') and mirror.HashKey not in audio.tags
    assert repr(audio.tags[foreign.HashKey])==foreign_before
    assert media.audio_identity(path)==before_identity


@LINUX
@TOOLS
def test_poweramp_companion_collision_is_rejected_before_any_journal(make_audio,roots,backup_dir):
    path=make_audio();audio=MP3(path)
    audio.tags.add(USLT(encoding=3,lang='und',desc='Phonoteka',text='Different owner lyrics'));audio.save()
    before=path.read_bytes()
    with pytest.raises(media.MediaError) as error:
        media.apply_changes(path,{},LRC,media.sha256_file(path),backup_dir,roots)
    assert error.value.code=='lyrics_conflict'
    assert path.read_bytes()==before and not list(backup_dir.rglob('*.json'))


@LINUX
@TOOLS
def test_changed_companion_invalidates_frozen_slot_selection(make_audio,roots,backup_dir):
    path=make_audio();media.apply_changes(path,{},LRC,media.sha256_file(path),backup_dir,roots)
    audio=MP3(path);inventory=metadata_slots.inventory(audio)
    slot=next(x for x in inventory['slots'] if x['kind']=='lyrics' and x['editable'])
    selection={'inventory_sha256':inventory['inventory_sha256'],'lyrics_slot_id':slot['id']}
    audio.tags[slot['mirror_identity']].text='Owner changed only the companion';audio.save()
    before=path.read_bytes()
    with pytest.raises(media.MediaError) as error:
        media.apply_changes(path,{},NEW,media.sha256_file(path),backup_dir,roots,slot_selection=selection)
    assert error.value.code=='slot_conflict' and path.read_bytes()==before
