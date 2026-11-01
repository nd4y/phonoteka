"""GAIN04 uses server-owned membership and verified audio, not album-name guesses."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid

from fastapi.testclient import TestClient
import pytest

from phonoteka import album_gain_policy as policy, db, media
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, job, drain
from test_album_loudness import fixture


def prepared(settings, monkeypatch):
    one, sha1 = seed(settings, 'one.mp3')
    two, sha2 = seed(settings, 'two.mp3')
    monkeypatch.setattr(media, 'audio_identity', lambda path: 'synthetic-packets:' + media.sha256_file(path))
    items = [{'track_id': one, 'sha256': sha1}, {'track_id': two, 'sha256': sha2}]
    context = policy.build(settings, items, {})
    with db.transaction(settings.db_path) as conn:
        policy.register(conn, context)
        candidate = {'id': 'album-replaygain:synthetic:' + sha1, 'provider': 'local_replaygain', 'kind': 'loudness',
                     'tags': {'replaygain_album_gain': '-2.00 dB', 'replaygain_album_peak': '0.90000000'},
                     'album_gain_group': {'id': context['id'], 'context_sha256': context['context_sha256']}}
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (one, sha1, db.encode({'candidates': [candidate]}), db.utcnow()))
    return one, two, sha1, context, candidate


def select(client, headers, tid, sha, candidate):
    return client.post(f'/api/tracks/{tid}/analysis/select', json={'candidate_ids': [candidate['id']]}, headers={**headers, 'If-Match': sha})


def test_group_change_blocks_selection_and_saved_proposal_enqueue(settings, monkeypatch):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        one, two, sha, context, candidate = prepared(settings, monkeypatch)
        proposal = select(client, headers, one, sha, candidate)
        assert proposal.status_code == 201, proposal.text
        payload = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (proposal.json()['id'],))['payload'])
        assert payload['field_sources']['replaygain_album_gain']['album_gain_group']['id'] == context['id']
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('e' * 64, two))
        rejected = select(client, headers, one, sha, candidate)
        assert rejected.status_code == 409 and rejected.json()['code'] == 'album_gain_stale'
        enqueue = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [one], 'params': {'proposal_id': proposal.json()['id']}}, headers={**headers, 'Idempotency-Key': 'stale-group'})
        assert enqueue.status_code == 409 and enqueue.json()['code'] == 'album_gain_stale'
        assert not db.fetchone(settings.db_path, "SELECT id FROM jobs WHERE kind='apply'")


def test_group_effect_checks_other_actual_file_even_if_database_is_unchanged(settings, monkeypatch):
    settings = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        one, _, sha, _, candidate = prepared(settings, monkeypatch)
        proposal = select(client, headers, one, sha, candidate).json()
        queued = job(client, headers, 'apply', [one], {'proposal_id': proposal['id']})
        original = (settings.work_root / 'one.mp3').read_bytes()
        (settings.work_root / 'two.mp3').write_bytes(b'external replacement after queue')
        drain(settings)
        result = client.get('/api/jobs/' + queued['id']).json()
        assert result['state'] == 'conflict', result
        assert (settings.work_root / 'one.mp3').read_bytes() == original
        assert not db.fetchone(settings.db_path, "SELECT id FROM history WHERE kind='apply'")


def test_new_overlapping_group_invalidates_old_group_but_retains_context(settings, monkeypatch):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        one, _, sha, context, _ = prepared(settings, monkeypatch)
        subset = policy.build(settings, [{'track_id': one, 'sha256': sha}], {})
        with db.transaction(settings.db_path) as conn:
            policy.register(conn, subset)
            with pytest.raises(media.MediaError) as failure:
                policy.validate(settings, conn, {'id': context['id'], 'context_sha256': context['context_sha256']}, one)
            assert failure.value.code == 'album_gain_stale'
            assert policy.saved(conn, 'album_gain_group:' + context['id']) == context
        assert client.get('/api/v1/album-gain-groups/' + context['id']).json()['state'] == 'stale'


def test_known_tag_only_commit_does_not_invalidate_other_member(settings, monkeypatch):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        one, two, _, context, _ = prepared(settings, monkeypatch)
        (settings.work_root / 'two.mp3').write_bytes(b'same packets with changed synthetic tag bytes')
        after = media.sha256_file(settings.work_root / 'two.mp3')
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE tracks SET sha256=? WHERE id=?', (after, two))
            conn.execute('INSERT INTO history VALUES(?,?,?,?,?,?,?,?)', (str(uuid.uuid4()), two, None, 'apply', context['members'][1]['measured_file_revision'], after, db.encode({'audio_identity': context['members'][1]['audio_identity']}), db.utcnow()))
            assert policy.validate(settings, conn, {'id': context['id'], 'context_sha256': context['context_sha256']}, one, verify_files=True)['id'] == context['id']


def test_release_subset_version_is_part_of_group_context(settings, monkeypatch):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        one, _, sha, _, _ = prepared(settings, monkeypatch)
        review_id = str(uuid.uuid4())
        review = {'version': 1, 'release_id': str(uuid.uuid4()), 'expected_slots': ['1:1'], 'note': 'Owner selected subset'}
        monkeypatch.setattr(policy.release_review, 'report', lambda conn, value: {'state': 'subset_complete', 'matched_track_ids': [one]})
        with db.transaction(settings.db_path) as conn:
            conn.execute('INSERT INTO settings VALUES(?,?)', ('release_review:' + review_id, db.encode(review)))
        context = policy.build(settings, [{'track_id': one, 'sha256': sha}], {'release_review_id': review_id, 'release_review_version': 1})
        with db.transaction(settings.db_path) as conn:
            policy.register(conn, context)
            conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode({**review, 'version': 2, 'expected_slots': None}), 'release_review:' + review_id))
            with pytest.raises(media.MediaError) as failure:
                policy.validate(settings, conn, {'id': context['id'], 'context_sha256': context['context_sha256']}, one)
            assert failure.value.code == 'album_gain_release_stale'


def test_real_album_candidates_apply_sequentially_without_false_stale_after_tag_write(settings):
    settings = replace(settings, read_only=False, storage_verified=True)
    fixture(settings, 'one.flac', 1, 1)
    fixture(settings, 'two.flac', .5, 1)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        job(client, headers, 'scan'); drain(settings)
        tracks = client.get('/api/tracks').json()['items']
        ids = [track['id'] for track in tracks]
        measured = job(client, headers, 'replaygain_album', ids); drain(settings)
        assert client.get('/api/jobs/' + measured['id']).json()['state'] == 'succeeded'
        proposals = []
        for track in tracks:
            detail = client.get('/api/tracks/' + track['id']).json()
            candidate = next(value for value in detail['analysis']['candidates'] if value['provider'] == 'local_replaygain')
            assert candidate['album_gain_group']['id']
            result = select(client, headers, track['id'], track['sha256'], candidate)
            assert result.status_code == 201, result.text
            proposals.append(result.json()['id'])
        for tid, proposal_id in zip(ids, proposals):
            applied = job(client, headers, 'apply', [tid], {'proposal_id': proposal_id}); drain(settings)
            assert client.get('/api/jobs/' + applied['id']).json()['state'] == 'succeeded'
            assert client.get('/api/tracks/' + tid).json()['tags']['replaygain_album_gain']


def test_real_concurrent_album_apply_waits_for_own_rename_and_catalog_commit(settings, monkeypatch):
    """Two jobs on one Worker cannot see each other's committed-file/old-DB gap."""
    settings = replace(settings, read_only=False, storage_verified=True)
    fixture(settings, 'one.flac', 1, 1)
    fixture(settings, 'two.flac', .5, 1)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        job(client, headers, 'scan'); drain(settings)
        tracks = client.get('/api/tracks').json()['items']
        ids = [track['id'] for track in tracks]
        measured = job(client, headers, 'replaygain_album', ids); drain(settings)
        assert client.get('/api/jobs/' + measured['id']).json()['state'] == 'succeeded'
        operations = []
        for track in tracks:
            detail = client.get('/api/tracks/' + track['id']).json()
            candidate = next(value for value in detail['analysis']['candidates'] if value['provider'] == 'local_replaygain')
            proposed = select(client, headers, track['id'], track['sha256'], candidate)
            assert proposed.status_code == 201, proposed.text
            queued = job(client, headers, 'apply', [track['id']], {'proposal_id': proposed.json()['id']})
            saved = db.fetchone(settings.db_path, 'SELECT * FROM jobs WHERE id=?', (queued['id'],))
            operations.append((saved, json.loads(saved['payload'])['items'][0]))
        worker = Worker(settings)
        renamed, release, second_started, second_entered = (threading.Event() for _ in range(4))
        persist, inner = worker.persist_mutation, worker._process_item
        first_job, first_item = operations[0]
        second_job, second_item = operations[1]

        def pause_before_catalog(operation, item, result, metadata=None):
            if item['track_id'] == first_item['track_id']:
                renamed.set()
                if not release.wait(20):
                    raise RuntimeError('Concurrent fixture failed to release the committed first file')
            return persist(operation, item, result, metadata)

        def observed_inner(operation, item):
            if item['track_id'] == second_item['track_id']:
                second_entered.set()
            return inner(operation, item)

        def second():
            second_started.set()
            return worker.process_item(second_job, second_item)

        monkeypatch.setattr(worker, 'persist_mutation', pause_before_catalog)
        monkeypatch.setattr(worker, '_process_item', observed_inner)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(worker.process_item, first_job, first_item)
                try:
                    assert renamed.wait(20), 'First real FLAC mutation did not reach the durable file commit'
                    # Prove the precise race window: new bytes, still old DB SHA.
                    assert media.sha256_file(settings.work_root / first_item['relative_path']) != first_item['sha256']
                    assert db.fetchone(settings.db_path, 'SELECT sha256 FROM tracks WHERE id=?', (first_item['track_id'],))['sha256'] == first_item['sha256']
                    waiting = pool.submit(second)
                    assert second_started.wait(5)
                    assert not second_entered.wait(.2), 'Second Album Gain validation entered the half-persisted group'
                    second_backups = settings.state_dir / 'backups' / second_item['track_id'] / second_job['id']
                    assert not list(second_backups.rglob('*.json'))
                finally:
                    release.set()
                assert first.result(timeout=30)['state'] == 'committed'
                assert waiting.result(timeout=30)['state'] == 'committed'
            assert second_entered.is_set()
            assert len(db.fetchall(settings.db_path, "SELECT id FROM history WHERE kind='apply'")) == 2
            for _, item in operations:
                current = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (item['track_id'],))
                assert current['sha256'] == media.sha256_file(settings.work_root / item['relative_path'])
                assert current['sha256'] != item['sha256'] and current['status'] == 'accepted'
        finally:
            release.set()
            worker.pool.shutdown(wait=True)
