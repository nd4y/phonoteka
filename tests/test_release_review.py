import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from phonoteka import db, media, release_review
from phonoteka.app import create_app
from phonoteka.providers import Providers
from phonoteka.worker import Worker
from test_backend import settings, client, authenticated, seed

RELEASE = '11111111-1111-4111-8111-111111111111'
OTHER_RELEASE = '22222222-2222-4222-8222-222222222222'


def document():
    return {'id': RELEASE, 'title': 'Specific edition', 'date': '2001-01-01', 'country': 'GB',
        'artist-credit': [{'name': 'Various Artists'}], 'media': [
            {'position': 1, 'track-count': 1, 'tracks': [{'id': 'release-track-one', 'position': 1, 'number': '1', 'title': 'First song', 'length': 100000,
                'recording': {'id': 'recording-one', 'artist-credit': [{'name': 'First artist'}]}}]},
            {'position': 2, 'track-count': 1, 'tracks': [{'id': 'release-track-two', 'position': 1, 'number': '1', 'title': 'Second song', 'length': 120000,
                'recording': {'id': 'recording-two', 'artist-credit': [{'name': 'Second artist'}]}}]},
        ]}


def members():
    result = []
    for index, (title, artist, duration) in enumerate([('First song', 'First artist', 100), ('Second song', 'Second artist', 120)], 1):
        result.append({'id': str(index), 'relative_path': f'cd{index}/01.flac', 'sha256': str(index) * 64,
            'metadata': db.encode({'duration': duration, 'tags': {'album': ['Same album label'], 'title': [title], 'artist': [artist], 'albumartist': ['Various Artists'], 'discnumber': [f'{index}/2'], 'tracknumber': ['01/01'], 'musicbrainz_albumid': [RELEASE], 'musicbrainz_trackid': ['recording-one' if index == 1 else 'recording-two']}})})
    return result


def test_release_reference_multidisc_compilation_and_truncation():
    ref = release_review.reference(document(), RELEASE)
    assert ref['discs'] == 2 and [track['slot'] for track in ref['tracks']] == ['1:1', '2:1']
    assert [track['artist'] for track in ref['tracks']] == ['First artist', 'Second artist']
    truncated = document(); truncated['media'][1]['track-count'] = 2
    with pytest.raises(media.MediaError, match='неполон'):
        release_review.reference(truncated, RELEASE)
    with pytest.raises(media.MediaError, match='другой релиз'):
        release_review.reference(document(), OTHER_RELEASE)
    duplicate = document(); duplicate['media'][1]['position'] = 1
    with pytest.raises(media.MediaError):
        release_review.reference(duplicate, RELEASE)


def test_album_label_does_not_prove_release_and_unknown_is_not_missing():
    ref = release_review.reference(document(), RELEASE)
    values = members()
    assert release_review.compare(ref, values)['state'] == 'complete'
    assert not release_review.compare(ref, values)['identity_verified_by_audio']
    wrong = deepcopy(values)
    for row in wrong:
        meta = json.loads(row['metadata']); meta['tags'] = {'album': ['Same album label']}; row['metadata'] = db.encode(meta)
    result = release_review.compare(ref, wrong)
    assert result['state'] == 'unknown' and not result['complete_release']
    assert result['counts']['unresolved'] == 2
    missing = release_review.compare(ref, values[:1])
    assert missing['state'] == 'missing' and missing['missing_slots'] == ['2:1']
    subset = release_review.compare(ref, values[:1], ['1:1'])
    assert subset['state'] == 'subset_complete' and not subset['complete_release'] and subset['intentional_subset']
    assert subset['tracks'][1]['state'] == 'not_expected'


def test_release_conflicts_duplicates_and_edition_extras_are_distinct():
    ref = release_review.reference(document(), RELEASE)
    values = members()
    duplicate = release_review.compare(ref, values + [{**values[0], 'id': 'duplicate', 'relative_path': 'copy.flac'}])
    assert duplicate['state'] == 'needs_review' and duplicate['tracks'][0]['state'] == 'duplicate'
    other = deepcopy(values)
    meta = json.loads(other[0]['metadata']); meta['tags']['musicbrainz_albumid'] = [OTHER_RELEASE]; meta['tags']['musicbrainz_trackid'] = ['wrong-recording']; meta['duration'] = 110; other[0]['metadata'] = db.encode(meta)
    conflict = release_review.compare(ref, other)
    assert {'different_release_id', 'recording_id_differs', 'duration_differs'} <= set(conflict['tracks'][0]['files'][0]['reasons'])
    assert not conflict['complete_release']
    extra = deepcopy(values[0]); extra['id'] = 'extra'; extra['relative_path'] = 'bonus.flac'
    meta = json.loads(extra['metadata']); meta['tags']['tracknumber'] = ['99']; meta['tags'].pop('musicbrainz_trackid'); extra['metadata'] = db.encode(meta)
    result = release_review.compare(ref, values + [extra])
    assert result['state'] == 'needs_review' and result['counts']['extra'] == 1 and result['counts']['missing'] == 0


def seed_release_member(settings):
    track_id, sha = seed(settings)
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track_id,)).fetchone()
        meta = json.loads(row['metadata']); meta.update(duration=100)
        meta['tags'].update(title=['First song'], artist=['First artist'], discnumber=['1'], tracknumber=['1'], album=['Specific edition'], musicbrainz_albumid=[RELEASE])
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track_id))
    return track_id, sha


def enqueue(owner, headers, track_id, key):
    response = owner.post('/api/jobs', headers={**headers, 'Idempotency-Key': key}, json={'kind': 'release_audit', 'track_ids': [track_id], 'params': {'release_id': RELEASE}})
    assert response.status_code == 202, response.text
    return response.json()['id']


def run(settings, transport):
    worker = Worker(settings)
    worker.providers = Providers(settings, transport=transport)
    asyncio.run(worker.run(once=True))


def test_release_job_requires_metadata_consent_and_explicit_valid_release(client, settings):
    headers = authenticated(client, settings); track_id, _ = seed(settings)
    response = client.post('/api/jobs', headers={**headers, 'Idempotency-Key': 'no-consent'}, json={'kind': 'release_audit', 'track_ids': [track_id], 'params': {'release_id': RELEASE}})
    assert response.status_code == 409 and response.json()['code'] == 'provider_disabled'
    cfg = replace(settings, external_metadata=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as owner:
        headers = authenticated(owner, cfg)
        bad = owner.post('/api/jobs', headers={**headers, 'Idempotency-Key': 'bad-release'}, json={'kind': 'release_audit', 'track_ids': [track_id], 'params': {'release_id': 'album name'}})
        assert bad.status_code == 422
        missing = owner.post('/api/jobs', headers={**headers, 'Idempotency-Key': 'no-members'}, json={'kind': 'release_audit', 'params': {'release_id': RELEASE}})
        assert missing.status_code == 422


def test_release_job_cache_subset_csv_and_revision_bound_decisions(settings):
    cfg = replace(settings, external_metadata=True)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == '/ws/2/release/' + RELEASE
        assert dict(request.url.params) == {'inc': 'recordings+artist-credits', 'fmt': 'json'}
        return httpx.Response(200, json=document())
    transport = httpx.MockTransport(handler)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as owner:
        headers = authenticated(owner, cfg); track_id, sha = seed_release_member(cfg)
        job_id = enqueue(owner, headers, track_id, 'first-release')
        run(cfg, transport)
        result = owner.get('/api/release-reviews/' + job_id).json()
        assert result['state'] == 'missing' and result['missing_slots'] == ['2:1']
        assert result['expected_slots'] is None and not result['intentional_subset']
        assert result['members'][0]['sha256'] == sha
        subset = owner.put('/api/release-reviews/' + job_id, headers=headers, json={'base_version': 1, 'expected_slots': ['1:1'], 'note': 'Only the first disc is in my collection'})
        assert subset.status_code == 200, subset.text
        assert subset.json()['state'] == 'subset_complete' and not subset.json()['complete_release']
        stale = owner.put('/api/release-reviews/' + job_id, headers=headers, json={'base_version': 1, 'expected_slots': None})
        assert stale.status_code == 412
        exported = owner.get('/api/release-reviews/' + job_id + '/export/csv')
        assert exported.status_code == 200 and 'not_expected' in exported.text
        second = enqueue(owner, headers, track_id, 'same-release-again')
        run(cfg, transport)
        assert owner.get('/api/release-reviews/' + second).status_code == 200
        assert len(calls) == 1 and db.fetchone(cfg.db_path, "SELECT COUNT(*) AS n FROM provider_calls WHERE provider='musicbrainz'")['n'] == 1
        first_value = json.loads(db.setting(cfg.db_path, release_review.PREFIX + job_id))
        second_value = json.loads(db.setting(cfg.db_path, release_review.PREFIX + second))
        assert first_value['usage']['expires_at'] == second_value['usage']['expires_at']
        assert media.sha256_file(cfg.work_root / 'a.mp3') == sha
        with db.transaction(cfg.db_path) as conn:
            conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('f' * 64, track_id))
        assert owner.get('/api/release-reviews/' + job_id).json()['state'] == 'unknown'
        assert owner.put('/api/release-reviews/' + job_id, headers=headers, json={'base_version': 2, 'expected_slots': None}).status_code == 412


def test_release_provider_wait_is_durable_and_does_not_finish_attempted_files(settings):
    cfg = replace(settings, external_metadata=True)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '60'}) if len(calls) == 1 else httpx.Response(200, json=document())
    transport = httpx.MockTransport(handler)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as owner:
        headers = authenticated(owner, cfg); track_id, _ = seed_release_member(cfg)
        job_id = enqueue(owner, headers, track_id, 'wait-release')
        run(cfg, transport)
        first = owner.get('/api/jobs/' + job_id).json()
        assert first['state'] == 'queued' and first['stage'] == 'waiting_provider'
        assert first['completed'] == first['failed'] == 0 and first['retry_at'] > time.time()
        with db.transaction(cfg.db_path) as conn:
            payload = json.loads(conn.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()['payload']); payload['retry_at'] = 0
            conn.execute('UPDATE jobs SET payload=? WHERE id=?', (db.encode(payload), job_id))
            conn.execute("INSERT OR REPLACE INTO settings VALUES('provider_breaker:musicbrainz','{}')")
        run(cfg, transport)
        assert owner.get('/api/jobs/' + job_id).json()['state'] == 'succeeded'
        assert len(calls) == 2


def test_expired_reference_is_unknown_and_redacts_provider_titles(settings):
    cfg = replace(settings, external_metadata=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as owner:
        headers = authenticated(owner, cfg); track_id, _ = seed_release_member(cfg)
        job_id = enqueue(owner, headers, track_id, 'expiry-release')
        run(cfg, httpx.MockTransport(lambda request: httpx.Response(200, json=document())))
        value = json.loads(db.setting(cfg.db_path, release_review.PREFIX + job_id)); value['usage']['expires_at'] = time.time() - 1
        db.set_setting(cfg.db_path, release_review.PREFIX + job_id, db.encode(value))
        result = owner.get('/api/release-reviews/' + job_id)
        assert result.status_code == 200 and result.json()['state'] == 'unknown'
        assert 'reference' not in result.json() and 'First song' not in result.text
        assert result.json()['summary'] == {'state': 'unknown'}
        token = owner.post('/api/tokens', headers=headers, json={'name': 'reader', 'scopes': ['read']}).json()['token']
        assert owner.get('/api/release-reviews/' + job_id, headers={'Authorization': 'Bearer ' + token}).status_code == 200
        denied = owner.put('/api/release-reviews/' + job_id, headers={'Authorization': 'Bearer ' + token}, json={'base_version': 1, 'expected_slots': None})
        assert denied.status_code == 403
