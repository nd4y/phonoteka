import base64
from copy import deepcopy
import io
import json
import sqlite3
import time
import uuid

import pytest
from PIL import Image

from phonoteka import db, provider_policy, release_review
from test_backend import settings, client, authenticated, seed


def image_data(color='red'):
    output = io.BytesIO(); Image.new('RGB', (16, 16), color).save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()


def body_for(client, ids):
    context = client.get('/api/album-batches/context?ids=' + ','.join(ids))
    assert context.status_code == 200, context.text
    items = context.json()['items']
    return {'track_ids': ids, 'revisions': {item['id']: item['sha256'] for item in items}, 'base_proposals': {item['id']: item['base_proposal_id'] for item in items}, 'tags': {'album': 'Owner album'}}


def preview(client, headers, body):
    response = client.post('/api/album-batches/preview', headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def save(client, headers, body, token, key='save-batch'):
    return client.post('/api/album-batches/save', headers={**headers, 'Idempotency-Key': key}, json={**body, 'preview_token': token})


def test_batch_owner_preview_atomic_save_idempotency_and_no_audio_jobs(client, settings):
    ids = [seed(settings, name)[0] for name in ('one.mp3', 'two.mp3')]
    assert client.get('/api/album-batches/context?ids=' + ','.join(ids)).status_code == 401
    headers = authenticated(client, settings); body = body_for(client, ids)
    result = preview(client, headers, body)
    assert result['saveable'] and len(result['plans']) == 2
    assert all('payload' not in plan for plan in result['plans'])
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals')['n'] == 0
    saved = save(client, headers, body, result['preview_token'])
    assert saved.status_code == 201, saved.text
    manifest = saved.json()
    assert len({item['proposal_id'] for item in manifest['entries']}) == 2
    assert not manifest['audio_changed'] and not manifest['apply_job_created']
    assert save(client, headers, body, result['preview_token']).json() == manifest
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals')['n'] == 2
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM jobs')['n'] == 0
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM history')['n'] == 0
    assert (settings.work_root / 'one.mp3').read_bytes() == b'not really audio'
    changed = {**body, 'tags': {'album': 'Another album'}}
    assert save(client, headers, changed, result['preview_token']).status_code == 409


def test_batch_stale_file_or_new_card_proposal_saves_nothing(client, settings):
    headers = authenticated(client, settings); ids = [seed(settings, name)[0] for name in ('one.mp3', 'two.mp3')]
    body = body_for(client, ids); plan = preview(client, headers, body)
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('a' * 64, ids[1]))
    assert save(client, headers, body, plan['preview_token']).status_code == 412
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals')['n'] == 0
    body = body_for(client, ids); plan = preview(client, headers, body)
    added = str(uuid.uuid4())
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (added, ids[1], 'a' * 64, db.encode({'tags': {'title': 'Other draft'}}), db.utcnow()))
    assert save(client, headers, body, plan['preview_token']).status_code == 412
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals')['n'] == 1
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals WHERE track_id=?', (ids[0],))['n'] == 0


def test_batch_database_failure_rolls_back_first_proposal_and_manifest(client, settings):
    headers = authenticated(client, settings); ids = [seed(settings, name)[0] for name in ('one.mp3', 'two.mp3')]
    body = body_for(client, ids); plan = preview(client, headers, body)
    with db.transaction(settings.db_path) as conn:
        conn.execute("CREATE TRIGGER fail_second_batch BEFORE INSERT ON proposals WHEN NEW.track_id='" + ids[1] + "' BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        save(client, headers, body, plan['preview_token'])
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals')['n'] == 0
    assert db.fetchone(settings.db_path, "SELECT COUNT(*) AS n FROM settings WHERE key LIKE 'album_batch:%'")['n'] == 0
    assert db.fetchone(settings.db_path, "SELECT COUNT(*) AS n FROM events WHERE kind='proposal_created'")['n'] == 0


def test_batch_locks_are_skipped_or_explicitly_rejected_without_group_override(client, settings):
    headers = authenticated(client, settings); first, sha = seed(settings, 'one.mp3'); second, _ = seed(settings, 'two.mp3')
    locked = client.put('/api/tracks/' + first + '/policy', headers={**headers, 'If-Match': sha}, json={'fields': ['album'], 'base_version': 0})
    assert locked.status_code == 200
    body = body_for(client, [first, second]); plan = preview(client, headers, body)
    assert plan['plans'][0]['skipped_fields'] == ['album'] and plan['plans'][0]['changes'] == []
    saved = save(client, headers, body, plan['preview_token'])
    assert saved.status_code == 201 and saved.json()['entries'][0]['state'] == 'unchanged'
    assert saved.json()['entries'][1]['state'] == 'proposed'
    body = body_for(client, [first, second]); body['lock_mode'] = 'reject'; plan = preview(client, headers, body)
    assert not plan['saveable'] and plan['plans'][0]['error']['code'] == 'field_locked'
    assert save(client, headers, body, plan['preview_token'], 'reject-lock').status_code == 409
    body['lock_mode'] = 'skip'; body['exceptions'] = {first: {'tags': {'album': 'Explicit illegal override'}}}
    plan = preview(client, headers, body)
    assert plan['plans'][0]['error']['code'] == 'field_locked'


def test_batch_keeps_foreign_lyrics_provenance_and_can_exclude_one_track(client, settings):
    headers = authenticated(client, settings); first, sha = seed(settings, 'one.mp3'); second, _ = seed(settings, 'two.mp3')
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO settings VALUES(?,?)', ('provider_usage:lrclib:lyrics', db.encode({'allow_cache': True, 'cache_ttl': 3600})))
        usage = provider_policy.rule(conn, 'lrclib', 'lyrics'); usage.update(origin='provider_acquired', acquired_at=time.time())
        payload = {'tags': {'title': 'Existing title proposal'}, 'lrc': '[00:01]Foreign lyrics',
            'sources': [{'id': 'lrclib:fixture', 'provider': 'lrclib', 'usage': usage}], 'field_sources': {'lrc': {'provider': 'lrclib', 'candidate_id': 'lrclib:fixture', 'usage': usage}}}
        old = str(uuid.uuid4()); conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (old, first, sha, db.encode(payload), db.utcnow()))
    body = body_for(client, [first, second]); body['exceptions'] = {second: {'exclude': True, 'reason': 'Different edition'}}
    plan = preview(client, headers, body); assert len(plan['plans']) == 1
    result = save(client, headers, body, plan['preview_token'])
    assert result.status_code == 201, result.text
    new_id = result.json()['entries'][0]['proposal_id']
    saved = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (new_id,))['payload'])
    assert saved['lrc'] == '[00:01]Foreign lyrics' and saved['tags']['title'] == 'Existing title proposal'
    assert saved['field_sources']['lrc']['provider'] == 'lrclib' and saved['sources'][0]['provider'] == 'lrclib'
    assert result.json()['excluded'] == [{'track_id': second, 'reason': 'Different edition'}]
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals WHERE track_id=?', (second,))['n'] == 0


def test_batch_cover_checks_and_manual_copy_cannot_launder_restricted_source(client, settings):
    headers = authenticated(client, settings); identity, sha = seed(settings)
    body = body_for(client, [identity]); body['cover'] = image_data()
    assert preview(client, headers, body)['saveable']
    mismatch = deepcopy(body); mismatch['cover'] = body['cover'].replace('image/png', 'image/jpeg')
    assert client.post('/api/album-batches/preview', headers=headers, json=mismatch).status_code == 422
    invalid = deepcopy(body); invalid['cover'] = 'data:image/png;base64,YWJj'
    assert client.post('/api/album-batches/preview', headers=headers, json=invalid).status_code == 422
    candidate = {'id': 'caa:restricted', 'provider': 'coverartarchive', 'kind': 'cover', 'cover': body['cover']}
    with db.transaction(settings.db_path) as conn:
        usage = provider_policy.candidate_usage(conn, candidate)
        provider_policy.register_values(conn, candidate, usage)
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (identity, sha, db.encode({'candidates': [candidate]}), db.utcnow()))
    plan = preview(client, headers, body)
    assert not plan['saveable'] and plan['plans'][0]['error']['code'] == 'provider_policy_denied'
    sourced = body_for(client, [identity]); sourced['cover_source'] = {'track_id': identity, 'candidate_id': candidate['id'], 'input_sha256': sha}
    response = client.post('/api/album-batches/preview', headers=headers, json=sourced)
    assert response.status_code == 409 and response.json()['code'] == 'provider_policy_denied'
    forged = deepcopy(body); forged['field_sources'] = {'cover': {'origin': 'owner_supplied'}}
    assert client.post('/api/album-batches/preview', headers=headers, json=forged).status_code == 422


def test_batch_policy_change_after_preview_requires_new_preview(client, settings):
    headers = authenticated(client, settings); identity, sha = seed(settings)
    body = body_for(client, [identity]); plan = preview(client, headers, body)
    assert client.put('/api/tracks/' + identity + '/policy', headers={**headers, 'If-Match': sha}, json={'fields': ['artist'], 'base_version': 0}).status_code == 200
    response = save(client, headers, body, plan['preview_token'])
    assert response.status_code == 412 and response.json()['code'] == 'batch_preview_stale'
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals')['n'] == 0


def test_batch_server_selected_cover_retains_source_and_preview_is_stable(client, settings):
    headers = authenticated(client, settings); identity, sha = seed(settings)
    image = image_data(); candidate = {'id': 'local:fixture', 'provider': 'local', 'kind': 'cover', 'cover': image, 'source_path': 'cover.png'}
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (identity, sha, db.encode({'candidates': [candidate]}), db.utcnow()))
    body = body_for(client, [identity]); body['cover_source'] = {'track_id': identity, 'candidate_id': candidate['id'], 'input_sha256': sha}
    first = preview(client, headers, body); second = preview(client, headers, body)
    assert first['preview_token'] == second['preview_token']
    shown = client.get('/api/album-batches/cover', params=body['cover_source'])
    assert shown.status_code == 200 and shown.headers['content-type'].startswith('image/png')
    assert shown.content == base64.b64decode(image.split(',', 1)[1])
    saved = save(client, headers, body, first['preview_token'])
    assert saved.status_code == 201, saved.text
    payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (saved.json()['entries'][0]['proposal_id'],))['payload'])
    assert payload['cover'] == image and payload['field_sources']['cover']['provider'] == 'local'
    assert payload['field_sources']['cover']['source_path'] == 'cover.png'
    assert payload['sources'][0]['id'] == 'local:fixture'


def test_batch_release_values_are_bound_to_server_review_provenance(client, settings):
    headers = authenticated(client, settings); identity, sha = seed(settings)
    release_id = '11111111-1111-4111-8111-111111111111'; review_id = str(uuid.uuid4())
    ref = release_review.reference({'id': release_id, 'title': 'Chosen edition', 'date': '2001', 'artist-credit': [{'name': 'Artist'}], 'media': [
        {'position': 1, 'track-count': 1, 'tracks': [{'position': 1, 'title': 'Example', 'length': 100000, 'recording': {'artist-credit': [{'name': 'Artist'}]}}]}]}, release_id)
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone(); meta = json.loads(row['metadata'])
        meta.update(duration=100); meta['tags'].update(tracknumber=['1'], discnumber=['1'])
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), identity))
        usage = provider_policy.rule(conn, 'musicbrainz', 'metadata'); usage.update(origin='provider_acquired', acquired_at=time.time(), expires_at=time.time() + 3600)
        value = {'id': review_id, 'version': 1, 'release_id': release_id, 'reference': ref, 'usage': usage, 'reference_state': 'available',
            'members': [{'track_id': identity, 'sha256': sha}], 'expected_slots': None, 'subset_note': '', 'created_at': db.utcnow(), 'updated_at': db.utcnow()}
        conn.execute('INSERT INTO settings VALUES(?,?)', (release_review.PREFIX + review_id, db.encode(value)))
    body = body_for(client, [identity]); body.update(tags={}, release_review_id=review_id, release_review_version=1, release_fields=['album', 'musicbrainz_albumid'])
    plan = preview(client, headers, body); assert plan['saveable']
    saved = save(client, headers, body, plan['preview_token'])
    assert saved.status_code == 201, saved.text
    payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (saved.json()['entries'][0]['proposal_id'],))['payload'])
    assert payload['tags']['album'] == 'Chosen edition' and payload['tags']['musicbrainz_albumid'] == release_id
    assert payload['field_sources']['album']['provider'] == 'musicbrainz'
    assert payload['field_sources']['album']['usage']['origin'] == 'provider_acquired'
    assert payload['album_batch_context']['release_review_id'] == review_id
