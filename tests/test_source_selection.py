import json

import pytest

from phonoteka import db, lyrics_document
from test_backend import settings, client, authenticated, seed
from test_field_policy import analysis, locks
from test_provider_policy import grant


def candidates():
    return [
        {'id': 'mb:1', 'kind': 'metadata', 'provider': 'musicbrainz', 'tags': {'title': 'Catalog title', 'album': 'Studio'}, 'match': {'reasons': []}},
        {'id': 'lrc:1', 'kind': 'lyrics', 'provider': 'lrclib', 'tags': {'title': 'Lyrics title'}, 'plain_lyrics': 'First line\nSecond line', 'match': {'reasons': []}},
    ]


def database_state(settings):
    with db.connect(settings.db_path) as conn:
        return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('settings', 'analyses', 'proposals', 'events', 'tracks')}


def test_preview_is_read_only_and_save_matches_reviewed_fields(client, settings):
    headers = authenticated(client, settings)
    tid, sha = seed(settings, lrc='Existing words')
    values = candidates()
    analysis(settings, tid, sha, values)
    with db.transaction(settings.db_path) as conn:
        grant(conn)
        grant(conn, kind='metadata')
        payload = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()['payload'])
        payload['alignment'] = {'words_revision': 'old', 'coverage': 0.4}
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(payload), tid))
    before = database_state(settings)
    params = {'candidate_ids': [c['id'] for c in values]}
    path = f'/api/tracks/{tid}/analysis/'
    response = client.post(path + 'preview', headers={**headers, 'If-Match': sha}, json=params)
    assert response.status_code == 200, response.text
    assert database_state(settings) == before  # Includes lazy document creation and alignment invalidation.
    preview = response.json()
    fields = {item['field']: item for item in preview['fields']}
    assert fields['title']['before'] == ['Example']
    assert fields['title']['after'] == 'Catalog title'
    assert fields['title']['source']['candidate_id'] == 'mb:1'
    assert fields['lrc']['after'] == 'First line\nSecond line'
    assert not preview['replaces_proposal']
    repeat = client.post(path + 'preview', headers={**headers, 'If-Match': sha}, json=params)
    assert repeat.json()['preview_revision'] == preview['preview_revision']
    params['preview_revision'] = preview['preview_revision']
    saved = client.post(path + 'select', headers={**headers, 'If-Match': sha}, json=params)
    assert saved.status_code == 201, saved.text
    proposal = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (saved.json()['id'],))['payload'])
    assert proposal['tags']['title'] == fields['title']['after']
    assert proposal['lrc'] == fields['lrc']['after']
    assert (settings.work_root / 'a.mp3').read_bytes() == b'not really audio'
    duplicate = client.post(path + 'select', headers={**headers, 'If-Match': sha}, json=params)
    assert duplicate.status_code == 412 and duplicate.json()['code'] == 'selection_preview_stale'


def test_selected_fields_preserve_tags_and_lyrics_omitted_in_comparison(client, settings):
    headers = authenticated(client, settings)
    tid, sha = seed(settings, lrc='Existing words')
    values = candidates()
    values[0]['tags']['musicbrainz_trackid'] = '6ee00e0a-7a48-4ca3-845f-18d851e7bf5a'
    analysis(settings, tid, sha, values)
    with db.transaction(settings.db_path) as conn:
        grant(conn)
        grant(conn, kind='metadata')
    path = f'/api/tracks/{tid}/analysis/'
    auth = {**headers, 'If-Match': sha}
    params = {'candidate_ids': [candidate['id'] for candidate in values]}
    comparison = client.post(path + 'preview', headers=auth, json=params)
    assert comparison.status_code == 200, comparison.text
    before = database_state(settings)
    full = {**params, 'preview_revision': comparison.json()['preview_revision']}
    for body in ({**params, 'excluded_fields': ['album']},
                 {**full, 'excluded_fields': ['album', 'album']},
                 {**full, 'excluded_fields': ['artist']},
                 {**full, 'excluded_fields': [f['field'] for f in comparison.json()['fields']]}):
        rejected = client.post(path + 'select', headers=auth, json=body)
        assert rejected.status_code == 422 and rejected.json()['code'] == 'invalid_selection_fields'
        assert database_state(settings) == before
    accepted = client.post(path + 'select', headers=auth,
                           json={**full, 'excluded_fields': ['musicbrainz_trackid', 'album', 'lrc']})
    assert accepted.status_code == 201, accepted.text
    proposal = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?',
                          (accepted.json()['id'],))['payload'])
    assert proposal['tags'] == {'title': 'Catalog title'}
    assert set(proposal['field_sources']) == {'title'} and [s['id'] for s in proposal['sources']] == ['mb:1']
    assert 'lrc' not in proposal and 'lyrics_document' not in proposal
    assert database_state(settings)['settings'] == before['settings']
    assert (settings.work_root / 'a.mp3').read_bytes() == b'not really audio'


def test_preview_reports_filtered_locks_and_invalid_lyrics(client, settings):
    headers = authenticated(client, settings)
    tid, sha = seed(settings)
    assert locks(client, headers, tid, sha, ['title']).status_code == 200
    values = candidates()
    values[1].pop('plain_lyrics')
    values[1]['lrc'] = '[00:10]Outside the five second fixture'
    analysis(settings, tid, sha, values)
    with db.transaction(settings.db_path) as conn:
        grant(conn)
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()
        metadata = json.loads(row['metadata']); metadata['duration'] = 5
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), tid))
    before = database_state(settings)
    response = client.post(f'/api/tracks/{tid}/analysis/preview', headers={**headers, 'If-Match': sha}, json={'candidate_ids': [c['id'] for c in values]})
    assert response.status_code == 200, response.text
    assert response.json()['blocked_fields'] == ['title']
    # Invalid timing is omitted by the same document checks used for selection.
    assert response.json()['omitted_fields'] == ['lrc']
    assert {field['field'] for field in response.json()['fields']} == {'album'}
    assert database_state(settings) == before


@pytest.mark.parametrize('change', ['analysis', 'priorities', 'proposal', 'document', 'locks'])
def test_review_cannot_be_saved_after_decisions_change(client, settings, change):
    headers = authenticated(client, settings)
    tid, sha = seed(settings)
    analysis(settings, tid, sha, candidates())
    path = f'/api/tracks/{tid}/analysis/'
    params = {'candidate_ids': ['mb:1']}
    preview = client.post(path + 'preview', headers={**headers, 'If-Match': sha}, json=params).json()
    with db.transaction(settings.db_path) as conn:
        if change == 'analysis':
            data = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()['payload'])
            data['candidates'][0]['tags']['album'] = 'Live'
            conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(data), tid))
        elif change == 'priorities':
            conn.execute('INSERT INTO settings VALUES(?,?)', ('field_source_priorities', db.encode({'version': 1, 'priorities': {}})))
        elif change == 'proposal':
            conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('another', tid, sha, db.encode({'tags': {}}), db.utcnow()))
        elif change == 'document':
            row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
            lyrics_document.ensure(conn, row)
        else:
            conn.execute('INSERT INTO settings VALUES(?,?)', ('track_field_policy:' + tid, db.encode({'version': 1, 'locks': {}})))
    before = database_state(settings)
    params['preview_revision'] = preview['preview_revision']
    result = client.post(path + 'select', headers={**headers, 'If-Match': sha}, json=params)
    assert result.status_code == 412 and result.json()['code'] == 'selection_preview_stale'
    assert database_state(settings) == before


def test_preview_retains_auth_revision_candidate_and_kind_validation(client, settings):
    tid, sha = seed(settings)
    values = candidates()
    values.append({**values[0], 'id': 'mb:2'})
    analysis(settings, tid, sha, values)
    path = f'/api/tracks/{tid}/analysis/preview'
    assert client.post(path, json={'candidate_ids': ['mb:1']}).status_code == 401
    headers = authenticated(client, settings)
    assert client.post(path, json={'candidate_ids': ['mb:1']}).status_code == 403
    assert client.post(path, headers=headers, json={'candidate_ids': ['mb:1']}).status_code == 412
    for ids, code in [([], 422), (['invented'], 422), (['mb:1', 'mb:2'], 422)]:
        assert client.post(path, headers={**headers, 'If-Match': sha}, json={'candidate_ids': ids}).status_code == code
    before = database_state(settings)
    denied = client.post(path, headers={**headers, 'If-Match': sha}, json={'candidate_ids': ['lrc:1']})
    assert denied.status_code == 409 and denied.json()['code'] == 'provider_policy_denied'
    assert database_state(settings) == before
