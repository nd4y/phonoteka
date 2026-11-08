import json
from phonoteka import db
from test_backend import settings, client, authenticated, seed


def test_neighbors_require_auth_and_cross_catalog_pages(client, settings):
    ids = [seed(settings, f'album/{i:03}.mp3')[0] for i in range(42)]
    assert client.get(f'/api/tracks/{ids[29]}/neighbors').status_code == 401
    authenticated(client, settings)
    middle = client.get(f'/api/tracks/{ids[29]}/neighbors').json()
    assert middle == {'id': ids[29], 'position': 30, 'catalog_position': 30, 'total': 42,
                      'previous_id': ids[28], 'next_id': ids[30]}
    assert client.get(f'/api/tracks/{ids[0]}/neighbors').json()['previous_id'] is None
    assert client.get(f'/api/tracks/{ids[-1]}/neighbors').json()['next_id'] is None


def test_neighbors_use_same_escaped_unicode_search_and_filters(client, settings):
    a, _ = seed(settings, 'Музыка/100%_a.mp3')
    b, _ = seed(settings, 'Музыка/100%_b.mp3', art=True)
    c, _ = seed(settings, 'Музыка/100%_c.mp3')
    seed(settings, 'Музыка/100XXd.mp3')
    authenticated(client, settings)
    result = client.get(f'/api/tracks/{a}/neighbors', params={'search': 'музыка/100%_', 'filter': 'missing_art'}).json()
    assert result['total'] == 2 and result['next_id'] == c and result['previous_id'] is None
    assert client.get(f'/api/tracks/{b}/neighbors', params={'filter': 'missing_art'}).json()['position'] is None
    assert client.get(f'/api/tracks/{a}/neighbors', params={'filter': 'invented'}).status_code == 422
    assert client.get('/api/tracks/missing-id/neighbors').status_code == 404


def test_neighbors_skip_cue_missing_and_invalid_but_keep_catalog_position(client, settings):
    a, _ = seed(settings, '01.mp3')
    cue, _ = seed(settings, '02.cue')
    missing, _ = seed(settings, '03.mp3')
    invalid, _ = seed(settings, '04.mp3')
    z, _ = seed(settings, '05.mp3')
    with db.transaction(settings.db_path) as conn:
        metadata = json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?', (cue,)).fetchone()['metadata'])
        metadata['format'] = 'cue'
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), cue))
        conn.execute("UPDATE tracks SET status='missing' WHERE id=?", (missing,))
        metadata['format'] = 'mp3'; metadata['input_error'] = {'code': 'invalid_audio'}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), invalid))
    authenticated(client, settings)
    assert client.get(f'/api/tracks/{a}/neighbors').json()['next_id'] == z
    last = client.get(f'/api/tracks/{z}/neighbors').json()
    assert last['position'] == 2 and last['catalog_position'] == 5 and last['previous_id'] == a


def test_music_gap_filters_exclude_composites_and_spoken_genres_before_pagination(client, settings):
    first, _ = seed(settings, 'set/01.mp3', lrc='[00:01.00]line')
    mix, mix_revision = seed(settings, 'set/02.mp3')
    speech, _ = seed(settings, 'set/03.mp3')
    image, _ = seed(settings, 'set/04.mp3')
    last, revision = seed(settings, 'set/05.mp3', art=True)
    complete, _ = seed(settings, 'set/06.mp3', art=True, lrc='[00:01.00]line')
    seed(settings, 'another/07.mp3')
    with db.transaction(settings.db_path) as conn:
        updates = {
            first: {'tags': {'title': ['Song'], 'artist': ['Artist'], 'album': ['Album']}},
            mix: {'recording_classification': {'kind': 'possible_mix', 'input_sha256': mix_revision}},
            speech: {'tags': {'genre': ['Other', ' Подкаст ']}},
            image: {'cue_album_image': True},
            last: {'possible_mix': True, 'recording_classification': {
                'kind': 'single_recording', 'origin': 'owner', 'input_sha256': revision}},
            complete: {'tags': {'title': ['Song'], 'artist': ['Artist'], 'album': ['Album']}},
        }
        for track_id, update in updates.items():
            row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track_id,)).fetchone()
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?',
                         (db.encode({**json.loads(row['metadata']), **update}), track_id))
    authenticated(client, settings)
    params = {'search': 'set/', 'filter': 'music_gaps', 'page_size': 1}
    for page, expected in [(1, first), (2, last)]:
        result = client.get('/api/tracks', params={**params, 'page': page}).json()
        assert result['total'] == 2 and [track['id'] for track in result['items']] == [expected]
    neighbors = client.get(f'/api/tracks/{first}/neighbors', params=params).json()
    assert neighbors['total'] == 2 and neighbors['next_id'] == last
    for name, expected in [('music_missing_art', first), ('music_missing_tags', last),
                           ('music_missing_lyrics', last)]:
        result = client.get('/api/tracks', params={**params, 'filter': name}).json()
        assert result['total'] == 1 and [track['id'] for track in result['items']] == [expected]
    assert client.get('/api/tracks', params={**params, 'filter': 'all'}).json()['total'] == 6
