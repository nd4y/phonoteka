from dataclasses import replace
import base64
import hashlib
import io
import json

from fastapi.testclient import TestClient
import pytest

from phonoteka import db, field_policy, media
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, client, authenticated, seed


def locks(client, headers, track_id, sha, fields, version=0):
    return client.put(f'/api/tracks/{track_id}/policy', headers={**headers, 'If-Match': sha}, json={'fields': fields, 'base_version': version})


def analysis(settings, track_id, sha, candidates):
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track_id, sha, db.encode({'input_sha256': sha, 'candidates': candidates, 'sources': [], 'state': 'needs_review'}), db.utcnow()))


def test_field_policy_owner_revision_and_compare_and_swap(client, settings):
    track_id, sha = seed(settings)
    path = f'/api/tracks/{track_id}/policy'
    assert client.get(path).status_code == 401
    headers = authenticated(client, settings)
    assert client.get(path).json()['version'] == 0
    assert client.put(path, headers=headers, json={'fields': ['title'], 'base_version': 0}).status_code == 412
    assert locks(client, headers, track_id, sha, ['invented']).status_code == 422
    assert locks(client, headers, track_id, sha, ['title', 'title']).status_code == 422
    created = locks(client, headers, track_id, sha, ['title'])
    assert created.status_code == 200, created.text
    assert created.json()['version'] == 1 and created.json()['stale_fields'] == []
    assert locks(client, headers, track_id, sha, []).status_code == 412
    token = client.post('/api/tokens', headers=headers, json={'name': 'writer', 'scopes': ['read', 'jobs:write']}).json()['token']
    assert client.put(path, headers={'Authorization': 'Bearer ' + token, 'If-Match': sha}, json={'fields': [], 'base_version': 1}).status_code == 403
    assert client.get(path).json()['locks']['title']['value_sha256'] == field_policy.value_hash(['Example'])


def test_candidate_selection_respects_locked_tags_lyrics_and_missing_cover(client, settings):
    headers = authenticated(client, settings)
    track_id, sha = seed(settings, lrc='[00:01]Owner words')
    assert locks(client, headers, track_id, sha, ['title', 'lrc', 'cover']).status_code == 200
    candidates = [
        {'id': 'mb:one', 'provider': 'musicbrainz', 'kind': 'metadata', 'tags': {'title': 'Wrong', 'artist': 'New artist'}, 'match': {'reasons': []}},
        {'id': 'lrc:one', 'provider': 'lrclib', 'kind': 'lyrics', 'lrc': '[00:01]Other words', 'match': {'reasons': []}},
        {'id': 'caa:one', 'provider': 'coverartarchive', 'kind': 'cover', 'cover': 'data:image/png;base64,YWJj', 'match': {'reasons': []}},
    ]
    analysis(settings, track_id, sha, candidates)
    selected = client.post(f'/api/tracks/{track_id}/analysis/select', headers={**headers, 'If-Match': sha}, json={'candidate_ids': [candidate['id'] for candidate in candidates]})
    assert selected.status_code == 201, selected.text
    payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (selected.json()['id'],))['payload'])
    assert payload['tags'] == {'artist': 'New artist'} and 'cover' not in payload and 'lrc' not in payload
    assert payload['field_policy']['blocked_fields'] == ['cover', 'lrc', 'title']
    denied = client.post(f'/api/tracks/{track_id}/proposal', headers={**headers, 'If-Match': sha}, json={'tags': {'title': 'Wrong'}})
    assert denied.status_code == 409 and denied.json()['code'] == 'field_locked'
    allowed = client.post(f'/api/tracks/{track_id}/proposal', headers={**headers, 'If-Match': sha}, json={'tags': {'title': 'Example'}})
    assert allowed.status_code == 201


def test_source_priorities_choose_each_field_and_store_provenance(client, settings):
    headers = authenticated(client, settings)
    track_id, sha = seed(settings)
    initial = client.get('/api/policies/sources').json()
    assert initial['manual_first'] and not initial['automatic_apply']
    changed = client.put('/api/policies/sources', headers=headers, json={'base_version': 0, 'priorities': {'title': ['musicbrainz', 'discogs'], 'album': ['discogs', 'musicbrainz']}})
    assert changed.status_code == 200 and changed.json()['version'] == 1
    assert client.put('/api/policies/sources', headers=headers, json={'base_version': 0, 'priorities': {}}).status_code == 412
    assert client.put('/api/policies/sources', headers=headers, json={'base_version': 1, 'priorities': {'title': ['unknown']}}).status_code == 422
    candidates = [
        {'id': 'discogs:one', 'provider': 'discogs', 'kind': 'release', 'tags': {'title': 'Release title', 'album': 'Discogs album'}, 'source_url': 'https://www.discogs.com/release/1', 'match': {'reasons': []}},
        {'id': 'mb:one', 'provider': 'musicbrainz', 'kind': 'metadata', 'tags': {'title': 'Recording title', 'album': 'MB album'}, 'source_url': 'https://musicbrainz.org/recording/1', 'match': {'reasons': []}},
    ]
    analysis(settings, track_id, sha, candidates)
    selected = client.post(f'/api/tracks/{track_id}/analysis/select', headers={**headers, 'If-Match': sha}, json={'candidate_ids': [candidate['id'] for candidate in candidates]})
    assert selected.status_code == 201, selected.text
    payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (selected.json()['id'],))['payload'])
    assert payload['tags'] == {'title': 'Recording title', 'album': 'Discogs album'}
    assert payload['source_priority_version'] == 1
    assert payload['field_sources']['title']['candidate_id'] == 'mb:one'
    assert payload['field_sources']['album']['candidate_id'] == 'discogs:one'
    assert len(payload['field_sources']['album']['response_sha256']) == 64
    assert client.get(f'/api/tracks/{track_id}').json()['tags']['title'] == 'Example'


def test_lock_tracks_field_value_across_revision_and_requires_stale_resolution(client, settings):
    headers = authenticated(client, settings)
    track_id, sha = seed(settings)
    assert locks(client, headers, track_id, sha, ['title']).status_code == 200
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track_id,)).fetchone()
        meta = json.loads(row['metadata']); meta['tags']['artist'] = ['Changed only elsewhere']
        conn.execute('UPDATE tracks SET sha256=?,metadata=? WHERE id=?', ('2' * 64, db.encode(meta), track_id))
    assert client.get(f'/api/tracks/{track_id}/policy').json()['stale_fields'] == []
    assert locks(client, headers, track_id, sha, [], 1).status_code == 412
    with db.transaction(settings.db_path) as conn:
        meta['tags']['title'] = ['External change']
        conn.execute('UPDATE tracks SET sha256=?,metadata=? WHERE id=?', ('3' * 64, db.encode(meta), track_id))
    assert client.get(f'/api/tracks/{track_id}/policy').json()['stale_fields'] == ['title']
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
    with pytest.raises(media.MediaError, match='title'):
        field_policy.assert_apply(settings, row, {'tags': {'artist': 'Other'}})
    assert locks(client, headers, track_id, '3' * 64, [], 1).status_code == 200
    assert locks(client, headers, track_id, '3' * 64, ['title'], 2).status_code == 200
    assert client.get(f'/api/tracks/{track_id}/policy').json()['stale_fields'] == []


def test_multivalue_lock_does_not_silently_drop_secondary_value(client, settings):
    headers = authenticated(client, settings)
    track_id, sha = seed(settings)
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track_id,)).fetchone()
        meta = json.loads(row['metadata']); meta['tags']['artist'] = ['First', 'Second']
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track_id))
    assert locks(client, headers, track_id, sha, ['artist']).status_code == 200
    result = client.post(f'/api/tracks/{track_id}/proposal', headers={**headers, 'If-Match': sha}, json={'tags': {'artist': 'First'}})
    assert result.status_code == 409 and result.json()['code'] == 'field_locked'


def test_cover_lock_captures_actual_front_image_hash(client, settings):
    from mutagen.flac import FLAC, Picture
    from PIL import Image
    from test_backend import synthetic
    path = synthetic(settings)
    buffer = io.BytesIO(); Image.new('RGB', (12, 12), 'red').save(buffer, format='PNG')
    image = buffer.getvalue(); picture = Picture(); picture.type = 3; picture.mime = 'image/png'; picture.data = image
    audio = FLAC(path); audio.add_picture(picture); audio.save()
    worker = Worker(settings)
    try:
        track_id = worker.scan_one(path.name, None)['track_id']
    finally:
        worker.pool.shutdown()
    sha = media.sha256_file(path); headers = authenticated(client, settings)
    result = locks(client, headers, track_id, sha, ['cover'])
    assert result.status_code == 200, result.text
    expected = field_policy.value_hash(hashlib.sha256(image).hexdigest())
    assert result.json()['locks']['cover']['value_sha256'] == expected
    body = {'cover': 'data:image/png;base64,' + base64.b64encode(image).decode()}
    assert client.post(f'/api/tracks/{track_id}/proposal', headers={**headers, 'If-Match': sha}, json=body).status_code == 201
    buffer = io.BytesIO(); Image.new('RGB', (12, 12), 'blue').save(buffer, format='PNG')
    body = {'cover': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()}
    denied = client.post(f'/api/tracks/{track_id}/proposal', headers={**headers, 'If-Match': sha}, json=body)
    assert denied.status_code == 409 and denied.json()['code'] == 'field_locked'
    assert media.sha256_file(path) == sha


def test_old_proposal_cannot_bypass_later_lock_and_queued_write_blocks_lock_changes(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as owner:
        headers = authenticated(owner, cfg)
        track_id, sha = seed(cfg)
        old = owner.post(f'/api/tracks/{track_id}/proposal', headers={**headers, 'If-Match': sha}, json={'tags': {'title': 'Created before lock'}}).json()
        assert locks(owner, headers, track_id, sha, ['title']).status_code == 200
        denied = owner.post('/api/jobs', headers={**headers, 'Idempotency-Key': 'old-proposal'}, json={'kind': 'apply', 'track_ids': [track_id], 'params': {'proposal_id': old['id']}})
        assert denied.status_code == 409 and denied.json()['code'] == 'field_locked'
        worker = Worker(cfg)
        try:
            worker.authorize = lambda _job: None  # Exercise lock defense independently of credential/queue checks.
            with pytest.raises(media.MediaError, match='title'):
                worker.process_item({'kind': 'apply', 'id': 'old-direct', 'auth_epoch': int(db.setting(cfg.db_path, 'auth_epoch')), 'payload': '{}'}, {'track_id': track_id, 'relative_path': 'a.mp3', 'sha256': sha, 'proposal': {'tags': {'title': 'Direct old payload'}}})
        finally:
            worker.pool.shutdown()
        current = owner.post(f'/api/tracks/{track_id}/proposal', headers={**headers, 'If-Match': sha}, json={'tags': {'artist': 'Allowed'}})
        assert current.status_code == 201
        queued = owner.post('/api/jobs', headers={**headers, 'Idempotency-Key': 'allowed-proposal'}, json={'kind': 'apply', 'track_ids': [track_id], 'params': {'proposal_id': current.json()['id']}})
        assert queued.status_code == 202, queued.text
        blocked = locks(owner, headers, track_id, sha, [], 1)
        assert blocked.status_code == 409 and blocked.json()['code'] == 'track_mutation_pending'
        assert owner.post('/api/jobs/' + queued.json()['id'] + '/cancel', headers=headers).status_code == 200
        assert locks(owner, headers, track_id, sha, [], 1).status_code == 200
