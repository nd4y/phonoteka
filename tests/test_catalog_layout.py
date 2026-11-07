from copy import deepcopy
from pathlib import PurePosixPath
import uuid
import pytest
from phonoteka import catalog_layout, media, publication_paths


def entry(number=1, **values):
    return {'track_id':str(uuid.UUID(int=number)), 'path':'old/song.flac',
            'title':'Song','artist':'Artist','album':'Album', **values}


def test_natural_album_layout_and_input_preservation():
    item=entry(albumartist='Album Artist',tracknumber='3/12',discnumber='2/2')
    before=deepcopy(item)
    assert catalog_layout.audio_paths([item])[item['track_id']]=='Album Artist/Album/2-03 - Song.flac'
    assert item==before


def test_unknown_tags_use_folder_labels_and_original_stem_without_inventing_tags():
    item=entry(title='',artist='',album='',path='old/Recording 01.mp3')
    assert catalog_layout.audio_paths([item])[item['track_id']]=='Unknown Artist/Unknown Album/Recording 01.mp3'
    assert item['title']==item['artist']==item['album']==''
    assert catalog_layout.audio_paths([entry(albumartist='  ')])[entry()['track_id']].startswith('Artist/')


def test_unicode_names_and_equivalent_directory_spelling_are_deterministic():
    items=[entry(1,artist='Beyonce\u0301',title='Étoile'),entry(2,artist='BEYONCÉ',title='Autre')]
    paths=catalog_layout.audio_paths(items)
    assert paths==catalog_layout.audio_paths(list(reversed(items)))
    assert len({PurePosixPath(path).parent for path in paths.values()})==1
    assert any('Étoile' in path for path in paths.values())


def test_unsafe_metadata_reserved_names_and_long_unicode_stay_portable():
    items=[entry(artist='.. /CON',album='NUL.',title='a\\b:*?<>|\x00\u202e'),
           entry(2,artist='曲'*120,album='🎵'*120,title='é'*180,tracknumber='9999')]
    for path in catalog_layout.audio_paths(items).values():
        publication_paths.key(path)
        assert len(path.encode('utf-8'))<=220
        assert '\x00' not in path and '\u202e' not in path
        assert len(PurePosixPath(path).parts)==3
    assert catalog_layout.audio_paths([entry(album='CON')])[entry()['track_id']].split('/')[1]=='_CON'


def test_sanitized_and_truncated_directory_collisions_do_not_merge_albums():
    items=[entry(1,album='A/B'),entry(2,album='A\\B'),
           entry(3,album='A'*70+'x'),entry(4,album='A'*70+'y')]
    paths=catalog_layout.audio_paths(items)
    assert paths==catalog_layout.audio_paths(list(reversed(items)))
    assert len({PurePosixPath(path).parent for path in paths.values()})==4


def test_same_stem_across_extensions_and_case_has_distinct_sidecar_paths():
    items=[entry(1),entry(2,path='other.mp3',title='song'),entry(3,path='third.wav')]
    paths=catalog_layout.audio_paths(items)
    assert len({publication_paths.key(str(PurePosixPath(path).with_suffix('.lrc'))) for path in paths.values()})==3
    assert paths==catalog_layout.audio_paths(list(reversed(items)))


@pytest.mark.parametrize('source',['../bad.mp3','/bad.mp3','C:/bad.mp3','a\\bad.mp3','a//b.mp3','a/./b.mp3','a/b'])
def test_rejects_unsafe_source_paths(source):
    with pytest.raises(media.MediaError,match='source'):
        catalog_layout.audio_paths([entry(path=source)])


def test_duplicate_and_invalid_identity_fail_closed():
    with pytest.raises(media.MediaError):catalog_layout.audio_paths([entry(),entry()])
    with pytest.raises(media.MediaError):catalog_layout.audio_paths([entry(track_id='../escape')])
