"""Automatic acceptance never infers music-pilot success from synthetic tests.

Internal profile overrides below exercise the future permitted branch only in
this process. Production registry remains empty and HTTP cannot modify it.
"""
from dataclasses import replace
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from phonoteka import automatic_quality as quality, db, lyrics_document, media, provider_policy
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, accept_publication_review
from test_provider_policy import grant


def fixture(settings, client, headers):
    tid, sha = seed(settings, lrc='[00:00.100]Hello world\n[00:01.000]Good song')
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        metadata = json.loads(row['metadata']); metadata.update(duration=3, audit={'state': 'passed', 'sha256': sha, 'fixture': 'gate_dependencies_only'})
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), tid))
    accept_publication_review(client, headers, tid)
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (tid,))
    with db.transaction(settings.db_path) as conn:
        document = lyrics_document.ensure(conn, row)
        grant(conn, 'shazam', 'metadata')
        rid = '11111111-1111-4111-8111-111111111111'
        metadata_candidate = {'id': 'mb:' + rid, 'provider': 'musicbrainz', 'kind': 'metadata', 'recording_id': rid,
            'duration': 3.0, 'tags': {'title': 'Example', 'artist': 'Artist', 'musicbrainz_trackid': rid}, 'identification': 'text_search', 'match': {'state': 'candidate', 'reasons': []}}
        fingerprint = {'id': 'shazam:fixture', 'provider': 'shazam', 'kind': 'metadata', 'tags': {'title': 'Example', 'artist': 'Artist'},
            'identification': 'audio_fingerprint', 'recording_version_verified': False, 'match': {'state': 'fingerprint_match', 'reasons': ['recording_version_unverified']}}
        proposed = '[00:00.200]Hello world\n[00:01.200]Good song'
        lyric_candidate = {'id': 'alignment:' + 'f' * 64, 'provider': 'independent_asr', 'kind': 'lyrics', 'lrc': proposed,
            'match': {'state': 'needs_review', 'reasons': ['human_timing_review_required']}, 'timing_verified': False}
        lines = [{**line, 'start_ms': (200 if index == 0 else 1200), 'end_ms': (800 if index == 0 else 1800), 'state': 'aligned',
                  'probability': .95, 'minimum_token_probability': .9, 'low_probability_words': 0} for index, line in enumerate(document['lines'])]
        analysis = {'input_sha256': sha, 'candidates': [metadata_candidate, fingerprint, lyric_candidate], 'sources': [],
            'asr': {'remote_job_id': 'f' * 64, 'input_sha256': sha, 'text': 'Hello world Good song', 'language': 'en',
                    'engine': 'faster-whisper', 'model': 'small', 'model_revision': 'fixture'},
            'alignment': {'document_id': document['document_id'], 'words_revision': document['words_revision'], 'lines': lines,
                'method': quality.PROFILE['methods'][0], 'complete': True, 'coverage': 1.0, 'automatic_acceptance': False}}
        analysis = provider_policy.prepare_analysis(conn, analysis)
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (tid, sha, db.encode(analysis), db.utcnow()))
        remote_job = str(uuid.uuid4()); now = db.utcnow()
        conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,completed,created_at,updated_at,auth_epoch) VALUES(?,'align','succeeded','succeeded',?,1,1,?,?,?)",
            (remote_job, db.encode({'items': [{'track_id': tid, 'sha256': sha}], 'params': {}}), now, now, int(db.setting(settings.db_path, 'auth_epoch'))))
        conn.execute("INSERT INTO job_items VALUES(?,?,'succeeded',?)", (remote_job, tid, db.encode({'inference_job_id': 'f' * 64})))
    response = client.post(f'/api/tracks/{tid}/analysis/select', json={'candidate_ids': [metadata_candidate['id'], lyric_candidate['id']]}, headers={**headers, 'If-Match': f'"{sha}"'})
    assert response.status_code == 201, response.text
    proposal = response.json()
    identity = {'base_version': 0, 'recording_candidate_id': metadata_candidate['id'], 'fingerprint_candidate_id': fingerprint['id'],
        'performance_version': 'studio', 'edition_mode': 'recording_only', 'language': 'en', 'reason': 'Owner compared the exact recording and studio performance'}
    response = client.put(f'/api/tracks/{tid}/automatic-quality/identity', json=identity, headers={**headers, 'If-Match': f'"{sha}"'})
    assert response.status_code == 200, response.text
    return tid, sha, proposal['id']


def enabled_for_test(monkeypatch):
    monkeypatch.setitem(quality.PROFILE, 'enabled', True)
    monkeypatch.setattr(quality, 'pilot_validated', lambda _profile: True)


def preview(client, tid):
    response = client.get(f'/api/tracks/{tid}/automatic-quality')
    assert response.status_code == 200, response.text
    return response.json()


def rewrite_analysis(settings, tid, update):
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()
        data = json.loads(row['payload']); update(data)
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(data), tid))


def test_production_profile_blocks_even_structurally_valid_input_and_cannot_be_forged(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, sha, pid = fixture(cfg, client, headers)
        result = preview(client, tid)
        assert result['state'] == 'blocked' and not result['automatic_acceptance']
        assert set(result['reason_codes']) == {'automatic_apply_disabled', 'pilot_not_validated'}, result
        queued = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': {'proposal_id': pid, 'acceptance_mode': 'automatic'}}, headers={**headers, 'Idempotency-Key': 'default-auto'})
        assert queued.status_code == 409 and queued.json()['code'] == 'automatic_quality_blocked'
        assert client.post('/api/automatic-quality/profile', json={'verified': True, 'enabled': True}, headers=headers).status_code == 405
        forged = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {}, 'verified': True, 'automatic_acceptance': True}, headers={**headers, 'If-Match': f'"{sha}"'})
        assert forged.status_code == 422
        forged_job = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': {'acceptance_mode': 'automatic', 'verified': True}}, headers={**headers, 'Idempotency-Key': 'forged'})
        assert forged_job.status_code == 422
        malformed_mode = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': {'acceptance_mode': []}}, headers={**headers, 'Idempotency-Key': 'malformed-mode'})
        assert malformed_mode.status_code == 422


@pytest.mark.parametrize(('mutation', 'expected'), [
    (lambda data: data['asr'].update(text='completely unrelated alien words and different content'), 'independent_asr_contradiction'),
    (lambda data: data['alignment']['lines'][0].update(start_ms=None, end_ms=None, state='unaligned'), 'complete_reliable_timings_required'),
    (lambda data: data['alignment']['lines'][0].update(minimum_token_probability=.01, low_probability_words=1), 'line_quality_below_profile'),
    (lambda data: data['asr'].update(language='xx'), 'language_unsupported_or_unconfirmed'),
    (lambda data: data['alignment'].update(words_revision='old'), 'alignment_dependency_stale'),
    (lambda data: data['asr'].update(remote_job_id='not-a-server-result'), 'independent_asr_provenance_missing'),
    (lambda data: data['alignment'].update(lines=[None]), 'alignment_result_malformed'),
    (lambda data: data['alignment']['lines'][0].update(text=None, line_id={}), 'alignment_result_malformed'),
    (lambda data: data.update(possible_mix=True), 'possible_mix_requires_manual_review'),
    (lambda data: data.update(critical_conflicts=['unresolved recording contradiction']), 'analysis_conflict_unresolved'),
    (lambda data: data['candidates'][0].update(duration=40), 'recording_duration_conflict_or_unknown'),
])
def test_each_negative_condition_is_executed_even_when_music_pilot_is_missing(settings, mutation, expected):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, _, _ = fixture(cfg, client, headers)
        rewrite_analysis(cfg, tid, mutation)
        result = preview(client, tid)
        assert expected in result['reason_codes'] and 'pilot_not_validated' in result['reason_codes'], result


def test_hypothetical_validated_profile_passes_and_enqueue_freezes_dependencies(settings, monkeypatch):
    enabled_for_test(monkeypatch)
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, _, pid = fixture(cfg, client, headers)
        result = preview(client, tid)
        assert result['state'] == 'passed' and result['reason_codes'] == [], result
        response = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': {'proposal_id': pid, 'acceptance_mode': 'automatic'}}, headers={**headers, 'Idempotency-Key': 'validated-test-only'})
        assert response.status_code == 202, response.text
        queued = db.fetchone(cfg.db_path, 'SELECT * FROM jobs WHERE id=?', (response.json()['id'],))
        item = json.loads(queued['payload'])['items'][0]
        assert item['automatic_quality']['dependency_sha256'] == result['dependency_sha256']
        assert item['automatic_quality']['apply_payload_sha256'] == quality.digest(item['proposal'])
        quality.assert_frozen(cfg, db.fetchone(cfg.db_path, 'SELECT * FROM tracks WHERE id=?', (tid,)), item, queued)


@pytest.mark.parametrize('dependency', ['source_priority', 'provider_policy', 'profile', 'line_lock', 'field_lock', 'proposal', 'authorization', 'file_revision'])
def test_stale_dependencies_stop_worker_before_audio_effect(settings, monkeypatch, dependency):
    enabled_for_test(monkeypatch)
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, _, pid = fixture(cfg, client, headers)
        response = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': {'proposal_id': pid, 'acceptance_mode': 'automatic'}}, headers={**headers, 'Idempotency-Key': dependency})
        assert response.status_code == 202, response.text
        queued = db.fetchone(cfg.db_path, 'SELECT * FROM jobs WHERE id=?', (response.json()['id'],))
        item = json.loads(queued['payload'])['items'][0]
        with db.transaction(cfg.db_path) as conn:
            if dependency == 'source_priority': conn.execute("INSERT OR REPLACE INTO settings VALUES('field_source_priorities',?)", (db.encode({'version': 1, 'priorities': {}}),))
            elif dependency == 'provider_policy': grant(conn, 'musicbrainz', 'metadata', allow_embed=False)
            elif dependency == 'authorization': conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
            elif dependency == 'line_lock':
                state = json.loads(conn.execute('SELECT value FROM settings WHERE key=?', ('lyrics_document:' + tid,)).fetchone()['value'])
                line = state['lines'][0]; state['locks'][line['line_id']] = {'words_locked': False, 'timing_locked': True, 'start_ms': line['start_ms'], 'words_sha256': quality.digest(line['text'])}
                state['version'] += 1; conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(state), 'lyrics_document:' + tid))
            elif dependency == 'field_lock':
                conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('track_field_policy:' + tid, db.encode({'version': 1, 'locks': {'title': {'input_sha256': item['sha256'], 'value_sha256': quality.digest(['Example'])}}})))
            elif dependency == 'proposal':
                conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (str(uuid.uuid4()), tid, item['sha256'], db.encode({'tags': {'title': 'A newer manual proposal'}}), db.utcnow()))
        if dependency == 'profile': monkeypatch.setitem(quality.PROFILE, 'version', 2)
        if dependency == 'file_revision': (cfg.work_root / item['relative_path']).write_bytes(b'External file change without reindex')
        before = (cfg.work_root / item['relative_path']).read_bytes()
        monkeypatch.setattr(media, 'apply_changes', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('must not reach audio mutation')))
        worker = Worker(cfg)
        try:
            with pytest.raises(media.MediaError): worker.process_item(queued, item)
        finally: worker.pool.shutdown()
        assert (cfg.work_root / item['relative_path']).read_bytes() == before


def test_manual_workflow_and_analysis_only_token_cannot_claim_automatic(settings, monkeypatch):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, sha, pid = fixture(cfg, client, headers)
        monkeypatch.setattr(quality, 'freeze', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('manual does not call automatic gate')))
        response = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': {'proposal_id': pid}}, headers={**headers, 'Idempotency-Key': 'manual'})
        assert response.status_code == 202, response.text
        item = json.loads(db.fetchone(cfg.db_path, 'SELECT payload FROM jobs WHERE id=?', (response.json()['id'],))['payload'])['items'][0]
        assert item['acceptance_mode'] == 'manual' and 'automatic_quality' not in item
        token = client.post('/api/tokens', json={'name': 'analysis', 'scopes': ['read', 'jobs:analysis']}, headers=headers).json()['token']
        denied = client.post('/api/jobs', json={'kind': 'apply', 'track_ids': [tid], 'params': {'acceptance_mode': 'automatic'}}, headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'token-auto'})
        assert denied.status_code == 403
        identity = client.put(f'/api/tracks/{tid}/automatic-quality/identity', json={'verified': True}, headers={**headers, 'If-Match': f'"{sha}"'})
        assert identity.status_code == 422


def test_manual_changed_values_and_recording_only_album_invention_are_not_automatic(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, sha, pid = fixture(cfg, client, headers)
        response = client.post(f'/api/tracks/{tid}/proposal', json={'tags': {'title': 'Made up title', 'album': 'Made up album'}, 'base_proposal_id': pid}, headers={**headers, 'If-Match': f'"{sha}"'})
        assert response.status_code == 201, response.text
        result = preview(client, tid)
        assert 'proposal_not_server_candidate' in result['reason_codes'] and 'recording_only_album_write' in result['reason_codes']
