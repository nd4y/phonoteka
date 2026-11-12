"""Source-backed subsets, actual atomic writes and their normal Ready follow-up."""
from dataclasses import replace
import base64
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from phonoteka import automatic_quality, confirmed_fields, db, input_readiness, media, provider_policy
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, synthetic, job, drain, accept_publication_review
from test_media import cover_bytes
from test_provider_policy import grant

RID = '11111111-1111-4111-8111-111111111111'
R1 = '22222222-2222-4222-8222-222222222222'
R2 = '33333333-3333-4333-8333-333333333333'


@pytest.fixture
def library(settings, request):
    cfg = replace(settings, read_only=False, storage_verified=True, input_grace_seconds=0)
    path = synthetic(cfg)
    from mutagen.flac import FLAC
    audio = FLAC(path); audio['PRIVATE_TEST_VALUE'] = ['keep this']; audio.save()
    if getattr(request, 'param', False):
        audio['LYRICS'] = ['[00:00.100]Known first line\n[00:01.000]Known second line']
        audio.save()
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        for _ in range(2):
            job(client, headers, 'scan'); drain(cfg)
        track, = client.get('/api/tracks').json()['items']
        tid = track['id']; sha = track['sha256']
        with db.transaction(cfg.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
            meta = json.loads(row['metadata']); meta['audit'] = media.audit_audio(path, cfg.roots)
            assert meta['audit']['state'] == 'passed'
            # Both real completed scans observed the same file; no sleep/grace in fixtures.
            assert input_readiness.describe(meta, grace_seconds=0)['state'] == 'ready'
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
            grant(conn, 'coverartarchive', 'cover')
            image = 'data:image/png;base64,' + base64.b64encode(cover_bytes()).decode()
            candidates = [
                {'id': 'acoustid:fixture:' + RID, 'provider': 'acoustid', 'kind': 'metadata',
                 'recording_id': RID, 'score': .999, 'identification': 'audio_fingerprint',
                 'tags': {'title': 'Original', 'artist': 'Artist', 'musicbrainz_trackid': RID},
                 'match': {'state': 'fingerprint_match', 'reasons': ['recording_version_unverified']}},
                {'id': 'mb:' + RID, 'provider': 'musicbrainz', 'kind': 'metadata', 'recording_id': RID,
                 'duration': meta['duration'], 'tags': {'title': 'Original', 'artist': 'Artist', 'musicbrainz_trackid': RID},
                 'releases': [{'id': r, 'title': 'Confirmed album', 'status': 'Official'} for r in (R1, R2)],
                 'match': {'state': 'matching_metadata', 'reasons': []}},
            ]
            for release in (R1, R2):
                candidates.extend([
                    {'id': 'mb-release:' + release, 'provider': 'musicbrainz', 'kind': 'release', 'release_id': release,
                     'tags': {'album': 'Confirmed album', 'musicbrainz_albumid': release, 'date': '2007'},
                     'match': {'state': 'needs_review', 'reasons': ['confirm_release']}},
                    {'id': 'caa:' + release, 'provider': 'coverartarchive', 'kind': 'cover', 'release_id': release,
                     'cover': image, 'match': {'state': 'needs_review', 'reasons': ['confirm_release']}},
                ])
            data = provider_policy.prepare_analysis(conn, {'input_sha256': sha, 'candidates': candidates,
                'sources': [], 'state': 'candidates', 'alignment': {'coverage': .12, 'complete': False},
                'asr': {'text': 'private synthetic unverified recognition', 'language': 'ru'}})
            conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)', (tid, sha, db.encode(data), db.utcnow()))
        yield cfg, client, headers, tid, sha, path


def plan(client):
    response = client.get('/api/confirmed-fields/plan')
    assert response.status_code == 200, response.text
    entry, = response.json()['items']
    return entry


def update_analysis(cfg, tid, change):
    with db.transaction(cfg.db_path) as conn:
        saved = conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()
        value = json.loads(saved['payload']); change(value)
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(value), tid))


def enqueue(client, headers, entry, *, publish=False, key=None):
    return client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [entry['track_id']],
        'params': {'acceptance_mode': 'confirmed_fields', 'field_tokens': {entry['track_id']: entry['token']},
                   'publish_confirmed': publish}}, headers={**headers, 'Idempotency-Key': key or str(uuid.uuid4())})


def test_metadata_plan_works_with_unvalidated_partial_asr_and_redacts_private_content(library):
    cfg, client, headers, tid, sha, path = library
    assert automatic_quality.profile()['enabled'] is False and not automatic_quality.PILOT_REGISTRY
    first = plan(client); second = plan(client)
    assert first['token'] == second['token'] and first['ready']
    assert {c['field'] for c in first['changes']} == {'album', 'cover'}
    assert first['remaining'] == [{'field': 'synced_lyrics', 'code': 'lyrics_review_required',
                                  'message': confirmed_fields.MESSAGES['lyrics_review_required']}]
    raw = json.dumps(first)
    assert 'private synthetic' not in raw and 'data:image/' not in raw and '_payload' not in first
    assert client.get('/api/automatic-quality/profile').json()['enabled'] is False


def test_actual_bulk_write_preserves_audio_draft_private_tags_and_native_rollback(library):
    cfg, client, headers, tid, sha, path = library
    source_sha = media.sha256_file(cfg.source_root/path.name)
    pending = '[00:00.000]Unverified words\n[00:01.000]Still need review'
    draft = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {}, 'lrc': pending},
                        headers={**headers, 'If-Match': sha})
    assert draft.status_code == 201, draft.text
    before = media.audio_identity(path)
    entry = plan(client); response = enqueue(client, headers, entry, key='stable-request')
    assert response.status_code == 202, response.text
    assert enqueue(client, headers, entry, key='stable-request').json()['id'] == response.json()['id']
    drain(cfg)
    completed = client.get('/api/jobs/' + response.json()['id']).json()
    assert completed['state'] == 'succeeded', completed
    current = client.get(f'/api/tracks/{tid}').json()
    assert current['tags']['album'] == 'Confirmed album' and current['has_art']
    assert not current.get('lrc') and media.audio_identity(path) == before
    assert current['proposal']['input_sha256'] == current['sha256']
    assert current['proposal']['lrc'] == pending and not current['proposal'].get('cover')
    from mutagen.flac import FLAC
    assert FLAC(path)['PRIVATE_TEST_VALUE'] == ['keep this']
    history = db.fetchone(cfg.db_path, "SELECT * FROM history WHERE track_id=? AND kind='apply'", (tid,))
    details = json.loads(history['details'])
    assert details['acceptance_mode'] == 'confirmed_fields' and details['automatic_acceptance']
    assert details['confirmed_fields']['fields'] == ['album', 'cover']
    assert media.sha256_file(cfg.source_root/path.name) != media.sha256_file(path)
    assert media.sha256_file(cfg.source_root/path.name) == source_sha
    assert media.sha256_file(__import__('pathlib').Path(details['backup_path'])) == sha
    rollback = job(client, headers, 'rollback', [tid], {'history_id': history['id']})
    drain(cfg)
    assert client.get('/api/jobs/' + rollback['id']).json()['state'] == 'succeeded'
    assert media.sha256_file(path) == sha


def test_existing_publication_profile_carries_only_unchanged_audio_lyrics_and_queues_ready(library):
    cfg, client, headers, tid, sha, path = library
    accepted = accept_publication_review(client, headers, tid)
    assert not accepted['human_timing_verified'] and not accepted['listened_sections']
    entry = plan(client); response = enqueue(client, headers, entry, publish=True)
    assert response.status_code == 202, response.text
    drain(cfg)
    current = client.get(f'/api/tracks/{tid}').json()
    review = client.get(f'/api/tracks/{tid}/lyrics-review').json()
    assert review['accepted_current_revision'] and review['acceptance_origin'] == confirmed_fields.CONTRACT
    assert not review['human_timing_verified'] and not review['listened_sections'] and not review['instrumental_confirmed']
    assert review['profile'] == accepted['profile'] and review['reason'] == accepted['reason']
    child, = db.fetchall(cfg.db_path, "SELECT * FROM jobs WHERE kind='publish'")
    assert child['state'] == 'succeeded', dict(child)
    assert json.loads(child['payload'])['confirmed_fields_parent'] == response.json()['id']
    ready = cfg.publication_root/'Ready'/path.name
    assert ready.is_file() and media.sha256_file(ready) == current['sha256']


def test_no_previous_acceptance_does_not_invent_one_or_publish(library):
    cfg, client, headers, tid, sha, path = library
    entry = plan(client); assert not entry['publication']['eligible']
    response = enqueue(client, headers, entry, publish=True)
    assert response.status_code == 202, response.text
    drain(cfg)
    assert client.get('/api/jobs/' + response.json()['id']).json()['state'] == 'succeeded'
    assert not db.fetchall(cfg.db_path, "SELECT id FROM jobs WHERE kind='publish'")
    assert not client.get(f'/api/tracks/{tid}/lyrics-review').json()['accepted_current_revision']


@pytest.mark.parametrize('change,missing,kept', [
    (lambda a: a['candidates'][-1].update(cover='data:image/png;base64,' + base64.b64encode(cover_bytes('blue')).decode()), 'cover', 'album'),
    (lambda a: a['candidates'][1]['releases'][1].update(title='Other album'), 'album', None),
])
def test_ambiguous_field_does_not_block_an_independent_field(library, change, missing, kept):
    cfg, client, headers, tid, sha, path = library
    update_analysis(cfg, tid, change)
    entry = plan(client)
    assert missing not in {c['field'] for c in entry['changes']}
    if kept: assert kept in {c['field'] for c in entry['changes']}
    assert any(r['field'] == missing for r in entry['remaining'])


def test_pending_manual_album_choice_also_blocks_dependent_cover(library):
    cfg, client, headers, tid, sha, path = library
    response = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'album': 'My different selection'}}, headers={**headers, 'If-Match': sha})
    assert response.status_code == 201, response.text
    entry = plan(client)
    assert any(r['field'] == 'album' and r['code'] == 'pending_choice' for r in entry['remaining'])
    # Art cannot be added for an unchosen different album merely because it is available.
    assert not any(c['field'] in {'album', 'cover'} for c in entry['changes'])


@pytest.mark.parametrize('mutation', ['analysis', 'proposal', 'policy', 'lock', 'document', 'profile', 'file'])
def test_worker_rechecks_every_dependency_before_effect(library, monkeypatch, mutation):
    cfg, client, headers, tid, sha, path = library
    entry = plan(client); response = enqueue(client, headers, entry)
    assert response.status_code == 202, response.text
    with db.transaction(cfg.db_path) as conn:
        if mutation == 'analysis':
            conn.execute("UPDATE analyses SET updated_at='changed' WHERE track_id=?", (tid,))
        elif mutation == 'proposal':
            conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (str(uuid.uuid4()), tid, sha, db.encode({'tags': {'album': 'other'}}), db.utcnow()))
        elif mutation == 'policy': grant(conn, 'coverartarchive', 'cover', allow_embed=False)
        elif mutation == 'lock':
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('track_field_policy:' + tid, db.encode({'version': 1, 'locks': {'album': {'value_sha256': field_policy_hash([]), 'input_sha256': sha}}})))
        elif mutation == 'document':
            from phonoteka import lyrics_document
            document = lyrics_document.ensure(conn, conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone())
            document['version'] += 1
            conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(document), lyrics_document.key(tid)))
        elif mutation == 'profile':
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('preparation_profile:' + tid, 'relaxed'))
    if mutation == 'file': path.write_bytes(path.read_bytes() + b'external-change')
    unchanged = media.sha256_file(path)
    drain(cfg)
    status = client.get('/api/jobs/' + response.json()['id']).json()
    assert status['state'] != 'succeeded'
    assert media.sha256_file(path) == unchanged
    assert not db.fetchall(cfg.db_path, "SELECT id FROM history WHERE kind='apply'")


def field_policy_hash(value):
    from phonoteka.field_policy import value_hash
    return value_hash(value)


@pytest.mark.parametrize('extra', [{'verified': True}, {'fields': ['lrc']}, {'publish_confirmed': 1}, {'proposal_id': 'client'}])
def test_client_cannot_assert_confidence_or_choose_unchecked_values(library, extra):
    cfg, client, headers, tid, sha, path = library
    entry = plan(client)
    params = {'acceptance_mode': 'confirmed_fields', 'field_tokens': {tid: entry['token']}, **extra}
    response = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': params}, headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
    assert response.status_code == 422, response.text
    assert media.sha256_file(path) == sha


@pytest.mark.parametrize('field,value,reason', [('possible_mix', True, 'excluded_recording'), ('genre', ['spoken word'], 'excluded_recording'), ('audit', None, 'audio_audit_required')])
def test_excluded_or_unchecked_recording_is_not_admitted(library, field, value, reason):
    cfg, client, headers, tid, sha, path = library
    with db.transaction(cfg.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()
        meta = json.loads(row['metadata'])
        if field == 'genre': meta['tags'][field] = value
        else: meta[field] = value
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
    entry = plan(client)
    assert not entry['ready'] and reason in entry['blocked']


def test_cancel_before_write_has_no_file_effect(library):
    cfg, client, headers, tid, sha, path = library
    response = enqueue(client, headers, plan(client))
    assert response.status_code == 202
    cancelled = client.post('/api/jobs/' + response.json()['id'] + '/cancel', headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    drain(cfg)
    assert media.sha256_file(path) == sha


def test_plan_pagination_is_bounded_and_requires_authentication(library):
    cfg, client, headers, tid, sha, path = library
    first = client.get('/api/confirmed-fields/plan?limit=1').json()
    assert first['scanned'] == 1 and first['next_after'] is None
    assert client.get('/api/confirmed-fields/plan?after=not-a-uuid').status_code == 422
    assert client.get('/api/confirmed-fields/plan?limit=1000').status_code == 422
    client.post('/api/auth/logout', headers=headers)
    assert client.get('/api/confirmed-fields/plan').status_code == 401


@pytest.mark.parametrize('bad', [[], 'invalid', {'candidates': [None]}, {'recognition_segments': [{'candidate': 'invalid'}]}])
def test_invalid_stored_evidence_is_not_admitted(library, bad):
    cfg, client, headers, tid, sha, path = library
    with db.transaction(cfg.db_path) as conn:
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(bad), tid))
    entry = plan(client)
    assert not entry['ready'] and 'invalid_evidence' in entry['blocked']
    assert media.sha256_file(path) == sha


def test_lock_on_album_blocks_dependent_cover(library):
    cfg, client, headers, tid, sha, path = library
    with db.transaction(cfg.db_path) as conn:
        conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('track_field_policy:' + tid,
            db.encode({'version': 1, 'locks': {'album': {'value_sha256': field_policy_hash([]), 'input_sha256': sha}}})))
    entry = plan(client)
    assert not entry['ready'] and any(x['code'] == 'field_locked' for x in entry['remaining'])


def test_noop_after_write_does_not_queue_another_effect(library):
    cfg, client, headers, tid, sha, path = library
    original = plan(client); response = enqueue(client, headers, original)
    assert response.status_code == 202
    drain(cfg)
    after = media.sha256_file(path)
    assert not plan(client)['ready']
    stale = enqueue(client, headers, original)
    assert stale.status_code == 409
    assert media.sha256_file(path) == after
    assert len(db.fetchall(cfg.db_path, "SELECT id FROM history WHERE kind='apply'")) == 1


def test_final_native_authorization_rechecks_new_evidence(library, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    entry = plan(client); response = enqueue(client, headers, entry)
    assert response.status_code == 202
    original = media.apply_changes
    def changed(*args, **kwargs):
        authorize = kwargs['authorize']
        def after_staging():
            with db.transaction(cfg.db_path) as conn:
                conn.execute("UPDATE analyses SET updated_at='changed-before-exchange' WHERE track_id=?", (tid,))
            authorize()
        kwargs['authorize'] = after_staging
        return original(*args, **kwargs)
    monkeypatch.setattr(media, 'apply_changes', changed)
    drain(cfg)
    assert media.sha256_file(path) == sha
    assert not db.fetchall(cfg.db_path, "SELECT id FROM history WHERE kind='apply'")
    status = client.get('/api/jobs/' + response.json()['id']).json()
    assert status['state'] != 'succeeded'


def test_parent_cancel_does_not_publish_already_written_work(library, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    accept_publication_review(client, headers, tid)
    response = enqueue(client, headers, plan(client), publish=True)
    assert response.status_code == 202
    original = Worker.persist_mutation
    def cancelled(self, task, item, result, metadata=None):
        value = original(self, task, item, result, metadata)
        if task['id'] == response.json()['id']:
            with db.transaction(cfg.db_path) as conn:
                conn.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (task['id'],))
        return value
    monkeypatch.setattr(Worker, 'persist_mutation', cancelled)
    drain(cfg)
    assert media.sha256_file(path) != sha
    assert not (cfg.publication_root/'Ready'/path.name).exists()
    child, = db.fetchall(cfg.db_path, "SELECT * FROM jobs WHERE kind='publish'")
    assert child['state'] == 'cancelled' and child['cancel_requested']


def test_publish_permission_is_checked_separately(library):
    cfg, client, headers, tid, sha, path = library
    response = client.post('/api/tokens', json={'name': 'metadata writer', 'scopes': ['read', 'files:write', 'jobs:create']}, headers=headers)
    assert response.status_code == 201, response.text
    auth = {'Authorization': 'Bearer ' + response.json()['token']}
    entry = plan(client)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as limited:
        # Planning is an owner-only UI action, not an automation-token API.
        assert limited.get('/api/confirmed-fields/plan', headers=auth).status_code == 403
        denied = enqueue(limited, auth, entry, publish=True)
        assert denied.status_code == 403, denied.text
    assert media.sha256_file(path) == sha


def test_native_commit_recovers_once_with_one_publication_followup(library, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    accept_publication_review(client, headers, tid)
    response = enqueue(client, headers, plan(client), publish=True)
    assert response.status_code == 202
    original = Worker.persist_mutation
    def interrupted(*args, **kwargs):
        raise OSError('synthetic catalog interruption after native commit')
    monkeypatch.setattr(Worker, 'persist_mutation', interrupted)
    drain(cfg)
    assert client.get('/api/jobs/' + response.json()['id']).json()['state'] == 'interrupted'
    written = media.sha256_file(path)
    assert written != sha
    monkeypatch.setattr(Worker, 'persist_mutation', original)
    drain(cfg)
    assert client.get('/api/jobs/' + response.json()['id']).json()['state'] == 'succeeded'
    drain(cfg)
    assert media.sha256_file(path) == written
    assert len(db.fetchall(cfg.db_path, "SELECT id FROM history WHERE kind='apply'")) == 1
    children = db.fetchall(cfg.db_path, "SELECT * FROM jobs WHERE kind='publish'")
    assert len(children) == 1 and children[0]['state'] == 'succeeded'
    assert media.sha256_file(cfg.publication_root/'Ready'/path.name) == written


@pytest.mark.parametrize('change', [
    lambda a: a['candidates'][1].update(releases_truncated=True),
    lambda a: a['candidates'][1]['releases'].append({'id': R1, 'title': 'Contradiction', 'status': 'Official'}),
    lambda a: a['candidates'].append({**a['candidates'][0], 'id': 'acoustid:other', 'recording_id': R2, 'score': .99}),
    lambda a: a['candidates'][0].update(score=.97),
    lambda a: a['candidates'][1].update(duration=900),
])
def test_incomplete_or_conflicting_proof_stays_pending(library, change):
    cfg, client, headers, tid, sha, path = library
    update_analysis(cfg, tid, change)
    assert not plan(client)['ready']
    assert media.sha256_file(path) == sha


@pytest.mark.parametrize('mutation', [None, 'offset', 'profile', 'provider', 'sample', 'identity', 'expired'])
def test_shazam_consensus_requires_real_separated_bound_samples_and_current_usage(library, mutation):
    # Pure proof-policy cases, not a claim that the two-second native fixture
    # contains these longer synthetic recognition samples.
    from phonoteka import recognition
    cfg, client, headers, tid, sha, path = library
    with db.transaction(cfg.db_path) as conn:
        grant(conn, 'shazam', 'metadata')
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()
        metadata = json.loads(row['metadata']); metadata['duration'] = 60
        data = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()['payload'])
        data['candidates'] = [c for c in data['candidates'] if c['provider'] != 'acoustid']
        data['candidates'][0]['duration'] = 60
        fingerprint = {'id': 'shazam:synthetic', 'provider': 'shazam', 'kind': 'metadata',
            'provider_recording_id': 'synthetic', 'tags': {'title': 'Original', 'artist': 'Artist'},
            'identification': 'audio_fingerprint', 'match': {'state': 'fingerprint_match'},
            'extractor_profile': recognition.POLICY, 'sample_sha256': 'b'*64, 'sample_offset': 15}
        data['candidates'].append(fingerprint)
        data = provider_policy.prepare_analysis(conn, data)
        fingerprint = data['candidates'][-1]
        data['recording_confirmation'] = 'agreement'
        data['recognition_segments'] = [{'state': 'match', 'provider': 'shazam', 'provider_recording_id': 'synthetic',
            'extractor_profile': recognition.POLICY, 'offset': offset, 'sample_sha256': sample*64}
            for offset, sample in [(0, 'a'), (15, 'b')]]
        if mutation == 'offset': data['recognition_segments'][0]['offset'] = -1
        elif mutation == 'profile': data['recognition_segments'][0]['extractor_profile'] = 'unknown'
        elif mutation == 'provider': data['recognition_segments'][0]['provider'] = 'different'
        elif mutation == 'sample': fingerprint['sample_sha256'] = 'c'*64
        elif mutation == 'identity': data['recognition_segments'][0]['provider_recording_id'] = 'other'
        elif mutation == 'expired': fingerprint['usage']['acquired_at'] = 0
        matches, fingerprints, reason = confirmed_fields.recording(conn, data, metadata)
        assert bool(matches) is (mutation is None)
        assert bool(reason) is (mutation is not None)


def test_explicit_selected_release_can_fill_its_album_without_choosing_another_edition(library):
    cfg, client, headers, tid, sha, path = library
    update_analysis(cfg, tid, lambda a: a['candidates'][1]['releases'][1].update(title='Another album'))
    with db.transaction(cfg.db_path) as conn:
        meta = json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()['metadata'])
        meta['tags']['musicbrainz_albumid'] = [R1]
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
    entry = plan(client)
    assert entry['ready'] and next(c['after'] for c in entry['changes'] if c['field'] == 'album') == 'Confirmed album'


def test_pending_album_change_blocks_art_for_existing_album(library):
    cfg, client, headers, tid, sha, path = library
    with db.transaction(cfg.db_path) as conn:
        meta = json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()['metadata'])
        meta['tags']['album'] = ['Confirmed album']
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
    response = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'album': 'Different pending album'}}, headers={**headers, 'If-Match': sha})
    assert response.status_code == 201
    entry = plan(client)
    assert not entry['ready'] and any(x['field'] == 'cover' and x['code'] == 'pending_choice' for x in entry['remaining'])


@pytest.mark.parametrize('library', [True], indirect=True)
def test_prior_actual_lyric_decisions_survive_identical_audio_and_text(library):
    cfg, client, headers, tid, sha, path = library
    before = client.get(f'/api/tracks/{tid}').json()
    assert before['lrc']
    accepted = accept_publication_review(client, headers, tid, profile='lyrics_required')
    assert accepted['human_timing_verified'] and accepted['listened_sections']
    response = enqueue(client, headers, plan(client), publish=True)
    assert response.status_code == 202, response.text
    drain(cfg)
    current = client.get(f'/api/tracks/{tid}').json()
    review = client.get(f'/api/tracks/{tid}/lyrics-review').json()
    assert current['sha256'] != sha and current['lrc'] == before['lrc']
    assert review['accepted_current_revision'] and review['human_timing_verified']
    assert review['decisions'] == accepted['decisions']
    assert review['listened_sections'] == accepted['listened_sections']
    assert review['words_revision'] == accepted['words_revision']
    assert review['timings_revision'] == accepted['timings_revision']
    assert media.sha256_file(cfg.publication_root/'Ready'/path.name) == current['sha256']
