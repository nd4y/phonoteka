from copy import deepcopy
from dataclasses import replace
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from phonoteka import db, lyrics_document, media, provider_policy
from phonoteka.app import create_app
from test_backend import authenticated, client, drain, job, seed, settings, synthetic


def get_document(client, track):
    response = client.get('/api/tracks/' + track + '/lyrics-document')
    assert response.status_code == 200, response.text
    return response.json()


def propose(client, headers, track, sha, document, lines=None):
    rows = deepcopy(document['lines'] if lines is None else lines)
    edited = {'base_version': document['version'], 'metadata': document['metadata'],
              'lines': [{key: line[key] for key in ('line_id', 'ordinal', 'text', 'start_ms')} for line in rows]}
    return client.post('/api/tracks/' + track + '/proposal', json={'tags': {}, 'lyrics_document': edited}, headers={**headers, 'If-Match': sha})


def line_lock(client, headers, track, sha, document, identity, *, words=False, timing=False):
    return client.put('/api/tracks/' + track + '/lyrics-document/locks',
                      json={'base_version': document['version'], 'lines': [{'line_id': identity, 'words_locked': words, 'timing_locked': timing}]},
                      headers={**headers, 'If-Match': sha})


def test_legacy_import_has_durable_occurrences_bom_offset_unknown_metadata_and_nullable(client, settings):
    track, sha = seed(settings, lrc='\ufeff[offset:500]\n[x-source:private test]\n[00:10][00:40]Припев\n[00:20]Куплет\nБез времени')
    authenticated(client, settings)
    first = get_document(client, track)
    assert [line['start_ms'] for line in first['lines']] == [9500, 19500, 39500, None]
    assert first['complete'] is False and first['metadata']['x-source'] == ['private test']
    ids = [line['line_id'] for line in first['lines']]
    assert len(set(ids)) == 4 and all(uuid.UUID(identity) for identity in ids)
    assert ids[0] != ids[2] and first['lines'][0]['text'] == first['lines'][2]['text']
    again = get_document(client, track)
    assert again['document_revision'] == first['document_revision'] and [line['line_id'] for line in again['lines']] == ids
    with TestClient(create_app(settings), base_url=settings.app_url) as restarted:
        authenticated(restarted, settings)
        assert [line['line_id'] for line in get_document(restarted, track)['lines']] == ids
    stored = json.loads(db.setting(settings.db_path, lyrics_document.key(track)))
    assert stored['embedded_sha256'] == lyrics_document.digest('\ufeff[offset:500]\n[x-source:private test]\n[00:10][00:40]Припев\n[00:20]Куплет\nБез времени')


def test_structured_words_and_time_edits_keep_ids_and_only_words_invalidate_alignment(client, settings):
    track, sha = seed(settings, lrc='[00:01]Припев\n[00:02]Куплет\n[00:03]Припев')
    headers = authenticated(client, settings); initial = get_document(client, track)
    transcript = {'state': 'draft', 'text': 'independent transcript retained', 'words': [{'word': 'Припев', 'start': 1, 'end': 1.5}]}
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track, sha, db.encode({'input_sha256': sha, 'asr': transcript,
                     'alignment': {'words_revision': initial['words_revision'], 'lines': [], 'stale': False}}), db.utcnow()))
    timed = deepcopy(initial['lines']); timed[0]['start_ms'] = 1100
    response = propose(client, headers, track, sha, initial, timed)
    assert response.status_code == 201, response.text
    time_doc = get_document(client, track)
    assert [line['line_id'] for line in time_doc['lines']] == [line['line_id'] for line in initial['lines']]
    assert time_doc['words_revision'] == initial['words_revision'] and time_doc['timings_revision'] != initial['timings_revision']
    data = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM analyses WHERE track_id=?', (track,))['payload'])
    assert data['alignment']['stale'] is False and data['asr'] == transcript
    assert propose(client, headers, track, sha, initial, timed).status_code == 412
    words = deepcopy(time_doc['lines']); words[2]['text'] = 'Второй припев: ручная правка'
    assert propose(client, headers, track, sha, time_doc, words).status_code == 201
    word_doc = get_document(client, track)
    assert [line['line_id'] for line in word_doc['lines']] == [line['line_id'] for line in initial['lines']]
    assert word_doc['words_revision'] != time_doc['words_revision'] and word_doc['timings_revision'] == time_doc['timings_revision']
    data = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM analyses WHERE track_id=?', (track,))['payload'])
    assert data['alignment']['stale_reason'] == 'lyrics_words_changed' and data['asr'] == transcript
    canonical = db.setting(settings.db_path, lyrics_document.key(track))
    assert 'Второй припев' not in canonical  # draft words are held only by the expirable proposal


def test_independent_line_locks_allow_other_axis_and_block_old_proposal(client, settings):
    track, sha = seed(settings, lrc='[00:01]Owner words\n[00:03]Other line')
    headers = authenticated(client, settings); initial = get_document(client, track)
    line_id = initial['lines'][0]['line_id']
    moved = deepcopy(initial['lines']); moved[0]['start_ms'] = 2000
    older = propose(client, headers, track, sha, initial, moved)
    assert older.status_code == 201
    current = get_document(client, track)
    assert propose(client, headers, track, sha, current, initial['lines']).status_code == 201
    current = get_document(client, track)
    assert line_lock(client, headers, track, sha, current, line_id, words=True).status_code == 200
    current = get_document(client, track)
    assert propose(client, headers, track, sha, current, moved).status_code == 201  # timing is independent of locked words
    current = get_document(client, track)
    assert propose(client, headers, track, sha, current, initial['lines']).status_code == 201
    current = get_document(client, track)
    assert line_lock(client, headers, track, sha, current, line_id, words=True, timing=True).status_code == 200
    locked = get_document(client, track)
    changed = deepcopy(locked['lines']); changed[0]['text'] = 'Overwrite locked words'
    rejected = propose(client, headers, track, sha, locked, changed)
    assert rejected.status_code == 409 and rejected.json()['code'] == 'lyrics_line_locked'
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as writer:
        writer_headers = authenticated(writer, cfg)
        denied = writer.post('/api/jobs', json={'kind': 'apply', 'track_ids': [track], 'params': {'proposal_id': older.json()['id']}},
                             headers={**writer_headers, 'Idempotency-Key': 'old-proposal-cannot-bypass-line-locks'})
        assert denied.status_code == 409 and denied.json()['code'] == 'lyrics_line_locked'
    assert get_document(client, track)['lines'] == locked['lines']


def test_lock_api_owner_csrf_version_and_partial_order_are_explicit(client, settings):
    track, sha = seed(settings, lrc='[00:01]One\n[00:02]Two')
    assert client.get('/api/tracks/' + track + '/lyrics-document').status_code == 401
    headers = authenticated(client, settings); document = get_document(client, track)
    url = '/api/tracks/' + track + '/lyrics-document/locks'
    payload = {'base_version': document['version'], 'lines': []}
    assert client.put(url, json=payload, headers={'If-Match': sha}).status_code == 403
    token = client.post('/api/tokens', headers=headers, json={'name': 'cannot-change-lyric-locks', 'scopes': ['read', 'jobs:write']}).json()['token']
    assert client.put(url, json=payload, headers={'Authorization': 'Bearer ' + token, 'If-Match': sha}).status_code == 403
    rows = deepcopy(document['lines']); rows[0]['start_ms'] = None
    assert propose(client, headers, track, sha, document, rows).status_code == 201
    partial = get_document(client, track)
    assert partial['complete'] is False and partial['lines'][0]['start_ms'] is None
    assert [line['line_id'] for line in partial['lines']] == [line['line_id'] for line in document['lines']]
    assert client.put(url, json=payload, headers={**headers, 'If-Match': sha}).status_code == 412
    rows = list(reversed(partial['lines']))
    for index, line in enumerate(rows): line['ordinal'] = index
    assert propose(client, headers, track, sha, partial, rows).status_code == 201
    reordered = get_document(client, track)
    assert [line['line_id'] for line in reordered['lines']] == [line['line_id'] for line in rows]


def test_real_flac_commit_preserves_document_ids_without_changing_audio_content(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    path = synthetic(cfg)
    original_audio = media.audio_identity(path)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); job(client, headers, 'scan'); drain(cfg)
        track = client.get('/api/tracks').json()['items'][0]
        document = get_document(client, track['id'])
        rows = [{'line_id': str(uuid.uuid4()), 'ordinal': 0, 'text': 'First owner line', 'start_ms': 100},
                {'line_id': str(uuid.uuid4()), 'ordinal': 1, 'text': 'Second owner line', 'start_ms': 1100}]
        response = propose(client, headers, track['id'], track['sha256'], document, rows)
        assert response.status_code == 201, response.text
        queued = job(client, headers, 'apply', [track['id']], {'proposal_id': response.json()['id']}); drain(cfg)
        result = client.get('/api/jobs/' + queued['id']).json()
        assert result['state'] == 'succeeded', result
        refreshed = get_document(client, track['id'])
        assert [line['line_id'] for line in refreshed['lines']] == [line['line_id'] for line in rows]
        assert all(line['words_state'] == line['timing_state'] == 'accepted' for line in refreshed['lines'])
        assert refreshed['source'] == 'embedded' and refreshed['file_revision'] == media.sha256_file(path)
        assert media.audio_identity(path) == original_audio


def test_expired_provider_lrc_removes_nested_proposed_words(client, settings, monkeypatch):
    track, sha = seed(settings, lrc='[00:01]Embedded words')
    headers = authenticated(client, settings); document = get_document(client, track)
    rows = deepcopy(document['lines']); rows[0]['text'] = 'Provider draft marker that must expire'
    response = propose(client, headers, track, sha, document, rows)
    payload = response.json()
    payload['field_sources'] = {'lrc': {'usage': {'origin': 'provider_acquired', 'provider': 'lrclib', 'data_type': 'lyrics'}}}
    monkeypatch.setattr(provider_policy, '_allowed', lambda *_: False)
    with db.connect(settings.db_path) as conn:
        cleaned = provider_policy.redact_payload(conn, payload)
    assert 'lrc' not in cleaned and 'lyrics_document' not in cleaned
    assert 'Provider draft marker' not in db.setting(settings.db_path, lyrics_document.key(track))
