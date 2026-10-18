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
