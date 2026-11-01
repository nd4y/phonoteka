import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from phonoteka import db, lyrics_review, media
from phonoteka.app import create_app
from test_backend import authenticated, drain, job, seed, settings


@pytest.fixture
def review_client(settings):
    app = create_app(settings)
    if not any(getattr(route, 'path', '') == '/api/tracks/{track_id}/lyrics-review' for route in app.routes):
        lyrics_review.install(app, settings)
    with TestClient(app, base_url=settings.app_url) as client:
        yield client


def prepared(settings, lrc='[00:01.000]Припев\n[00:05.000]Куплет\n[00:10.000]Припев'):
    track, sha = seed(settings, lrc=lrc, art=True)
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track,)).fetchone()
        meta = json.loads(row['metadata'])
        meta['tags']['album'] = ['Album']
        meta['duration'] = 20
        meta['audit'] = {'state': 'passed', 'sha256': sha, 'audio_identity': 'test-encoded-packets'}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track))
    return track, sha


def body(review, **changes):
    result = {'base_version': review['version'], 'document_revision': review['document_revision'],
              'profile': 'lyrics_required', 'lines': [], 'listened_sections': []}
    result.update(changes)
    return result


def test_review_requires_owner_csrf_and_exact_audio_revision(review_client, settings):
    track, sha = prepared(settings)
    url = f'/api/tracks/{track}/lyrics-review'
    assert review_client.get(url).status_code == 401
    headers = authenticated(review_client, settings)
    review = review_client.get(url).json()
    assert review['gate']['state'] == 'needs_review'
    assert review['human_timing_verified'] is False
    assert review['poweramp_verified'] is False
    assert review_client.put(url, json=body(review), headers={'If-Match': f'"{sha}"'}).status_code == 403
    assert review_client.put(url, json=body(review), headers={**headers, 'If-Match': '"stale"'}).status_code == 412
    assert review_client.put(url, json=body(review, document_revision='0' * 64), headers={**headers, 'If-Match': f'"{sha}"'}).status_code == 412
    token = review_client.post('/api/tokens', headers=headers, json={'name': 'automation-cannot-claim-listening', 'scopes': ['read', 'jobs:write']}).json()['token']
    assert review_client.get(url, headers={'Authorization': 'Bearer ' + token}).status_code == 403
    assert review_client.put(url, json=body(review), headers={'Authorization': 'Bearer ' + token, 'If-Match': sha}).status_code == 403


def test_manual_occurrence_review_and_three_listening_sections_precede_acceptance(review_client, settings):
    track, sha = prepared(settings)
    headers = {**authenticated(review_client, settings), 'If-Match': f'"{sha}"'}
    url = f'/api/tracks/{track}/lyrics-review'
    review = review_client.get(url).json()
    assert review['lines'][0]['text'] == review['lines'][2]['text']
    assert review['lines'][0]['line_id'] != review['lines'][2]['line_id']
    decisions = [{'line_id': line['line_id'], 'words_state': 'accepted', 'timing_state': 'accepted'} for line in review['lines']]
    incomplete = review_client.put(url, json=body(review, lines=decisions, accept_revision=True), headers=headers)
    assert incomplete.status_code == 409
    assert review_client.get(url).json()['version'] == 0  # rejected transaction is atomic
    response = review_client.put(url, json=body(review, lines=decisions, listened_sections=['start', 'middle', 'end'], accept_revision=True), headers=headers)
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted['gate']['state'] == 'passed'
    assert accepted['human_timing_verified'] is True
    assert accepted['accepted_current_revision'] is True
    assert accepted['poweramp_verified'] is False
    assert accepted['automatic_acceptance'] is False
    assert review_client.put(url, json=body(review), headers=headers).status_code == 412
    uncertain = decisions[0] | {'timing_state': 'uncertain'}
    response = review_client.put(url, json=body(accepted, lines=[uncertain], listened_sections=['start', 'middle', 'end']), headers=headers)
    assert response.status_code == 200
    assert response.json()['gate']['state'] == 'needs_review'
    assert response.json()['human_timing_verified'] is False
    history = db.fetchall(settings.db_path, "SELECT details FROM history WHERE track_id=? AND kind='lyrics_review'", (track,))
    assert len(history) == 2
    assert all(json.loads(row['details'])['music_changed'] is False for row in history)


def test_plain_partial_and_asr_draft_do_not_become_verified_from_timestamp_presence(review_client, settings):
    track, sha = prepared(settings, lrc='[00:01.000]Таймированная строка\nСтрока без времени')
    headers = {**authenticated(review_client, settings), 'If-Match': f'"{sha}"'}
    url = f'/api/tracks/{track}/lyrics-review'
    review = review_client.get(url).json()
    assert review['complete'] is False
    untimed = next(line for line in review['lines'] if line['start_ms'] is None)
    result = review_client.put(url, json=body(review, lines=[{'line_id': untimed['line_id'], 'words_state': 'accepted', 'timing_state': 'accepted'}]), headers=headers)
    assert result.status_code == 422
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track, sha, db.encode({'asr': {'state': 'draft', 'text': 'Модель сказала слова'}, 'alignment': {'complete': True}}), db.utcnow()))
    fresh = review_client.get(url).json()
    assert fresh['human_timing_verified'] is False
    assert 'complete_synced_lyrics_required' in fresh['gate']['reasons']


def test_word_timing_or_file_change_invalidates_listening_and_acceptance(review_client, settings):
    track, sha = prepared(settings)
    headers = {**authenticated(review_client, settings), 'If-Match': f'"{sha}"'}
    url = f'/api/tracks/{track}/lyrics-review'
    review = review_client.get(url).json()
    decisions = [{'line_id': line['line_id'], 'words_state': 'accepted', 'timing_state': 'accepted'} for line in review['lines']]
    accepted = review_client.put(url, json=body(review, lines=decisions, listened_sections=['start', 'middle', 'end'], accept_revision=True), headers=headers).json()
    original_document = accepted['document_revision']
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('b' * 64, track))
    fresh = review_client.get(url).json()
    assert fresh['stale'] is True and fresh['accepted_current_revision'] is False
    assert fresh['human_timing_verified'] is False and fresh['listened_sections'] == []
    assert all(line['timing_state'] == 'unknown' for line in fresh['lines'])
    assert fresh['document_revision'] != original_document
    assert review_client.put(url, json=body(accepted, lines=decisions), headers=headers).status_code == 412


def test_instrumental_and_relaxed_are_explicit_versioned_exemptions(review_client, settings):
    track, sha = prepared(settings, lrc='')
    headers = {**authenticated(review_client, settings), 'If-Match': f'"{sha}"'}
    url = f'/api/tracks/{track}/lyrics-review'
    review = review_client.get(url).json()
    assert review_client.put(url, json=body(review, profile='instrumental', instrumental_confirmed=True), headers=headers).status_code == 422
    assert review_client.put(url, json=body(review, profile='instrumental', reason='Прослушал всю запись'), headers=headers).status_code == 422
    result = review_client.put(url, json=body(review, profile='instrumental', reason='Прослушал всю запись: вокала нет', instrumental_confirmed=True, accept_revision=True), headers=headers)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value['gate']['state'] == 'passed'
    assert value['human_timing_verified'] is False
    assert value['gate']['exceptions'] == ['lyrics_not_required_owner_confirmed_instrumental']
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track,)).fetchone()
        meta = json.loads(row['metadata'])
        meta['has_art'] = False
        meta['tags'].pop('album')
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track))
    result = review_client.put(url, json=body(value, profile='relaxed', reason='Редкая запись без альбомной обложки, текст не найден', accept_revision=True), headers=headers)
    assert result.status_code == 200
    assert result.json()['gate']['state'] == 'passed'
    assert 'owner_exception:cover' in result.json()['gate']['exceptions']
    assert result.json()['gate']['exception_reason'].startswith('Редкая запись')


def test_relaxed_exception_cannot_bypass_failed_decode_conflict_or_provider_denial(review_client, settings, monkeypatch):
    track, sha = prepared(settings, lrc='')
    headers = {**authenticated(review_client, settings), 'If-Match': f'"{sha}"'}
    url = f'/api/tracks/{track}/lyrics-review'
    review = review_client.get(url).json()
    result = review_client.put(url, json=body(review, profile='relaxed', reason='Личное исключение без текста', accept_revision=True), headers=headers)
    assert result.status_code == 200
    assert result.json()['gate']['state'] == 'passed'
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track,)).fetchone()
        meta = json.loads(row['metadata']); meta['audit']['state'] = 'failed'
        conn.execute("UPDATE tracks SET metadata=?,status='conflict' WHERE id=?", (db.encode(meta), track))
    def denied(*_, **__):
        from phonoteka import media
        raise media.MediaError('provider_policy_denied', 'fixture denied')
    monkeypatch.setattr(lyrics_review.provider_policy, 'require_track', denied)
    gate = review_client.get(url).json()['gate']
    assert gate['state'] == 'blocked'
    assert {'critical_file_conflict', 'current_decode_audit_required', 'provider_export_denied'} <= set(gate['reasons'])


def accept_all(client, headers, track, sha):
    url = f'/api/tracks/{track}/lyrics-review'
    review = client.get(url).json()
    decisions = [{'line_id': line['line_id'], 'words_state': 'accepted', 'timing_state': 'accepted'} for line in review['lines']]
    accepted = client.put(url, json=body(review, lines=decisions, listened_sections=['start', 'middle', 'end'], accept_revision=True),
                          headers={**headers, 'If-Match': sha})
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def replace_saved_review(settings, track, **changes):
    with db.transaction(settings.db_path) as conn:
        value = lyrics_review.load(conn, track)
        value.update(changes)
        conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(value), 'lyrics_review:' + track))


def test_frozen_review_detects_changed_profile_decisions_policy_and_tampered_snapshot(review_client, settings, monkeypatch):
    track, sha = prepared(settings)
    headers = authenticated(review_client, settings)
    accepted = accept_all(review_client, headers, track, sha)
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track,))
    frozen = lyrics_review.freeze_publication(settings, row)
    assert frozen['profile'] == 'lyrics_required' and frozen['review_version'] == accepted['version']
    assert 'Припев' not in db.encode(frozen) and 'decisions' not in frozen
    gate = lyrics_review.assert_publish_allowed(settings, row, frozen=frozen)
    assert gate['state'] == 'passed' and gate['review_snapshot_sha256'] == frozen['snapshot_sha256']
    with pytest.raises(media.MediaError) as missing:
        lyrics_review.assert_publish_allowed(settings, row, frozen=None)
    assert missing.value.code == 'publication_snapshot_required'
    with pytest.raises(media.MediaError) as corrupted:
        lyrics_review.assert_publication_frozen(settings, row, {**frozen, 'profile': 'relaxed'})
    assert corrupted.value.code == 'publication_review_stale'
    with monkeypatch.context() as patch:
        patch.setattr(lyrics_review, 'POLICY_VERSION', 'future-listening-policy')
        with pytest.raises(media.MediaError) as stale_policy:
            lyrics_review.assert_publish_allowed(settings, row, frozen=frozen)
        assert stale_policy.value.code == 'publication_review_stale'
    replace_saved_review(settings, track, profile='relaxed', reason='A different owner decision', version=accepted['version'] + 1)
    assert lyrics_review.publication_gate(settings, row)['state'] == 'passed'
    with pytest.raises(media.MediaError) as changed:
        lyrics_review.assert_publish_allowed(settings, row, frozen=frozen)
    assert changed.value.code == 'publication_review_stale'


def test_queued_publish_cannot_pick_up_later_review_without_new_owner_job(settings, monkeypatch):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        track, sha = prepared(cfg)
        headers = authenticated(client, cfg)
        accepted = accept_all(client, headers, track, sha)
        queued = job(client, headers, 'publish', [track])
        item = json.loads(db.fetchone(cfg.db_path, 'SELECT payload FROM jobs WHERE id=?', (queued['id'],))['payload'])['items'][0]
        assert item['publication_review']['review_version'] == accepted['version']
        replace_saved_review(cfg, track, profile='relaxed', reason='Changed after queue approval', version=accepted['version'] + 1)
        def unexpected_audit(*_):
            raise AssertionError('Stale owner review must fail before expensive audit or filesystem effects')
        monkeypatch.setattr(media, 'audit_audio', unexpected_audit)
        before = (cfg.work_root / 'a.mp3').read_bytes()
        drain(cfg)
        result = client.get('/api/jobs/' + queued['id']).json()
        assert result['state'] == 'needs_review', result
        assert result['results'][0]['code'] == 'publication_review_stale'
        assert not (cfg.publication_root / 'Ready/a.mp3').exists()
        assert (cfg.work_root / 'a.mp3').read_bytes() == before


def test_publication_rechecks_frozen_review_at_real_copy_commit(settings, monkeypatch):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        track, sha = prepared(cfg)
        headers = authenticated(client, cfg)
        accepted = accept_all(client, headers, track, sha)
        queued = job(client, headers, 'publish', [track])
        monkeypatch.setattr(media, 'audit_audio', lambda *_: {'state': 'passed', 'sha256': sha, 'audio_identity': 'test-encoded-packets'})
        original_publish = media.publish
        def changed_after_audit(*args, **kwargs):
            replace_saved_review(cfg, track, profile='relaxed', reason='Changed before atomic commit', version=accepted['version'] + 1)
            return original_publish(*args, **kwargs)
        monkeypatch.setattr(media, 'publish', changed_after_audit)
        before = (cfg.work_root / 'a.mp3').read_bytes()
        drain(cfg)
        result = client.get('/api/jobs/' + queued['id']).json()
        assert result['results'][0]['code'] == 'publication_review_stale', result
        assert result['results'][0]['recovery_required'] is True  # real prepared journal remains diagnosable
        assert not (cfg.publication_root / 'Ready/a.mp3').exists()
        assert (cfg.work_root / 'a.mp3').read_bytes() == before
