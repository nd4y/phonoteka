import copy
from dataclasses import replace
import json
import uuid
import time

import pytest

from phonoteka import db, recording_review, review_queue, provider_attempts, recognition, provider_policy, release_review, media
from test_backend import authenticated, client, seed, drain, settings as base_settings


@pytest.fixture
def settings(base_settings):
    return replace(base_settings, external_fingerprints=True, external_metadata=True, inference_url='http://inference:8000')


def proposal(settings, tid, sha, payload, identity=None):
    identity = identity or str(uuid.uuid4())
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (identity, tid, sha, db.encode(payload), db.utcnow()))
    return identity


def mix(settings, tid, sha):
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()
        meta = recording_review.annotate(json.loads(row['metadata']), sha, True)
        meta.update(duration=240, audit={'state': 'passed', 'sha256': sha, 'profile': 'full_decode_all_audio_streams', 'audio_identity': 'a'*64})
        conn.execute("UPDATE tracks SET metadata=?,status='needs_review' WHERE id=?", (db.encode(meta), tid))
    return meta


def enqueue(client, headers, kind, items):
    return client.post('/api/jobs', json={'kind': kind, 'track_ids': [i['track_id'] for i in items],
        'params': {'review_tokens': {i['track_id']: i['token'] for i in items}}},
        headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})


def test_same_review_population_for_summary_filter_and_plan(client, settings):
    authenticated(client, settings)
    plain, sha = seed(settings, 'plain.mp3'); proposal(settings, plain, sha, {'tags': {}, 'lrc': 'Проверить слова\nЕщё строка'})
    empty, sha = seed(settings, 'empty.mp3'); proposal(settings, empty, sha, {'tags': {}})
    stale, sha = seed(settings, 'stale.mp3'); proposal(settings, stale, '0'*64, {'tags': {'title': 'old'}})
    mixed, sha = seed(settings, 'mix.mp3'); mix(settings, mixed, sha)
    error, sha = seed(settings, 'broken.mp3')
    with db.transaction(settings.db_path) as conn: conn.execute("UPDATE tracks SET status='error' WHERE id=?", (error,))
    summary = client.get('/api/summary').json(); plan = client.get('/api/review/plan').json()
    page = client.get('/api/tracks?filter=review').json()
    assert summary['needs_review'] == plan['counts']['total'] == page['total'] == 3
    assert client.get('/api/reports/collection').json()['counts']['review_required'] == page['total']
    assert {x['id'] for x in page['items']} == {plain, mixed, error}
    for category in review_queue.CATEGORIES:
        assert client.get('/api/tracks?filter=review_'+category).json()['total'] == plan['counts'][category]
    assert client.get('/api/tracks/'+empty).json()['review']['proposal_state'] == 'no_changes'
    assert client.get('/api/tracks/'+stale).json()['review']['proposal_state'] == 'stale'
    with db.connect(settings.db_path) as conn: assert conn.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 3


def test_only_latest_proposal_counts_and_multivalue_change_is_preserved(client, settings):
    authenticated(client, settings); tid, sha = seed(settings)
    proposal(settings, tid, sha, {'tags': {'title': ['Example', 'Variant']}})
    assert client.get('/api/tracks?filter=review').json()['total'] == 1
    proposal(settings, tid, sha, {'tags': {'title': ['Example']}})
    assert client.get('/api/tracks?filter=review').json()['total'] == 0


def test_preparation_freezes_reference_and_language_without_apply_and_survives_double_click(client, settings):
    headers = authenticated(client, settings); tid, sha = seed(settings)
    text = 'Проверить слова\nЕщё одна строка'
    pid = proposal(settings, tid, sha, {'tags': {}, 'lrc': text})
    batch = next(b for b in client.get('/api/review/plan').json()['batches'] if b['kind']=='align')
    response = enqueue(client, headers, 'align', batch['items']); assert response.status_code == 202, response.text
    again = enqueue(client, headers, 'align', batch['items']); assert again.status_code == 202, again.text
    assert again.json()['id'] == response.json()['id']
    with db.connect(settings.db_path) as conn:
        jobs = conn.execute('SELECT * FROM jobs').fetchall(); assert len(jobs) == 1 and jobs[0]['kind']=='align'
        item = json.loads(jobs[0]['payload'])['items'][0]
        assert item['reference_text'] == text and item['language_hint'] == 'ru'
        assert conn.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 1
        assert conn.execute('SELECT id FROM proposals').fetchone()[0] == pid
    cancelled = client.post('/api/jobs/'+response.json()['id']+'/cancel', headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()['state'] == 'cancelled'
    plan = client.get('/api/review/plan').json()
    assert not any(b['items'] for b in plan['batches'])
    assert plan['counts']['listen'] == 1
    assert 'отменена' in client.get('/api/tracks/'+tid).json()['review']['message']
    assert enqueue(client, headers, 'align', batch['items']).json()['id'] == response.json()['id']


def test_stale_plan_and_wrong_kind_cannot_create_jobs(client, settings):
    headers = authenticated(client, settings); tid, sha = seed(settings)
    proposal(settings, tid, sha, {'tags': {}, 'lrc': 'Слова без времени'})
    items = next(b['items'] for b in client.get('/api/review/plan').json()['batches'] if b['kind']=='align')
    assert enqueue(client, headers, 'apply', items).status_code == 422
    proposal(settings, tid, sha, {'tags': {}, 'lrc': 'Другие слова'})
    assert enqueue(client, headers, 'align', items).status_code == 409
    with db.connect(settings.db_path) as conn: assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_exhausted_recognition_budget_and_owner_classification_have_no_auto_action(client, settings):
    authenticated(client, settings); tid, sha = seed(settings); meta = mix(settings, tid, sha)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO settings VALUES(?,?)', (provider_attempts.RECOGNITION_PREFIX+'a'*64, db.encode({'calls': provider_attempts.RECOGNITION_CALL_LIMIT})))
    assert client.get('/api/tracks/'+tid).json()['review']['action'] is None
    with db.transaction(settings.db_path) as conn:
        conn.execute('DELETE FROM settings WHERE key=?', (provider_attempts.RECOGNITION_PREFIX+'a'*64,))
        meta['recording_classification']['origin'] = 'owner'
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
    assert client.get('/api/tracks/'+tid).json()['review']['category'] == 'listen'


def agreed():
    return {'state': 'agreement', 'segments': [{'state':'match','offset':offset,'sample_sha256':str(index)*64,
        'candidate':{'provider':'shazam','provider_recording_id':'same','isrc':'SAME'}}
        for index,offset in enumerate([45,156,216], 1)]}


@pytest.mark.parametrize('case', ['owner', 'unknown', 'partial', 'long', 'isrc', 'duplicate_sample', 'close_offsets'])
def test_disputed_recheck_keeps_block(case):
    meta = recording_review.annotate({}, 'a'*64, True); verdict = agreed(); duration=240
    if case=='owner': meta['recording_classification']['origin']='owner'
    if case=='unknown': verdict['segments'][2]['state']='unknown_outcome'
    if case=='partial': verdict['segments'].pop()
    if case=='long': duration=1200
    if case=='isrc': verdict['segments'][1]['candidate']['isrc']='OTHER'
    if case=='duplicate_sample': verdict['segments'][1]['sample_sha256']=verdict['segments'][0]['sample_sha256']
    if case=='close_offsets': verdict['segments'][2]['offset']=46; verdict['segments'][1]['offset']=47
    assert recording_review.possible_mix(recording_review.resolve_recheck(meta,'a'*64,verdict,duration),'a'*64)


def test_unanimous_machine_recheck_only_changes_classification():
    meta = recording_review.annotate({'tags':{'title':['Original']},'lrc':'original'}, 'a'*64, True)
    before=copy.deepcopy(meta); result=recording_review.resolve_recheck(meta,'a'*64,agreed(),240)
    assert meta==before and result['tags']==meta['tags'] and result['lrc']==meta['lrc']
    assert not recording_review.possible_mix(result,'a'*64)
    assert result['recording_classification']['origin']=='segment_recheck'
    assert 'publication' not in result


@pytest.mark.parametrize('outcome', ['agreement', 'conflict', 'manual_race'])
def test_worker_recheck_saves_evidence_without_applying_audio(client, settings, monkeypatch, outcome):
    headers = authenticated(client, settings); tid, sha = seed(settings); mix(settings, tid, sha)
    monkeypatch.setattr(recognition, 'sample', lambda path, roots, duration, offset=None: (str(offset).encode(), offset))
    async def recognize(providers, audio, offset, *args, **kwargs):
        identity = 'other' if outcome == 'conflict' and offset > 200 else 'same'
        if outcome == 'manual_race':
            with db.transaction(settings.db_path) as conn:
                row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()
                meta = json.loads(row['metadata']); meta['recording_classification']['origin'] = 'owner'
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
        return {'id': 'shazam:'+identity, 'provider': 'shazam', 'provider_recording_id': identity,
                'isrc': identity, 'kind': 'metadata', 'tags': {'title': 'Song', 'artist': 'Artist'},
                'identification': 'audio_fingerprint', 'match': {'state': 'fingerprint_match', 'reasons': []}}
    monkeypatch.setattr(recognition, 'recognize', recognize)
    items = next(b['items'] for b in client.get('/api/review/plan').json()['batches'] if b['kind']=='recognize')
    queued = enqueue(client, headers, 'recognize', items); assert queued.status_code == 202, queued.text
    drain(replace(settings, external_metadata=False))
    job = client.get('/api/jobs/'+queued.json()['id']).json()
    detail = client.get('/api/tracks/'+tid).json()
    if outcome == 'manual_race':
        assert job['failed'] == 1 and detail['possible_mix']
        assert not detail.get('analysis')
    else:
        assert job['completed'] == 1 and job['failed'] == 0, job
        assert detail['possible_mix'] is (outcome == 'conflict')
        assert len(detail['analysis']['recognition_segments']) == 3
    assert media.sha256_file(settings.work_root/'a.mp3') == sha
    with db.connect(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM jobs WHERE kind IN (\'apply\',\'publish\')').fetchone()[0] == 0


def test_policy_recovery_and_new_manual_classification_stop_preparation(client, settings):
    headers = authenticated(client, settings); tid, sha = seed(settings)
    usage = provider_policy.defaults('lrclib', 'lyrics')
    proposal(settings, tid, sha, {'tags': {}, 'lrc': 'Текст', 'field_sources': {'lrc': {'provider': 'lrclib', 'usage': usage}}})
    detail = client.get('/api/tracks/'+tid).json()
    assert detail['review']['action'] is None and detail['review']['category'] == 'listen'
    mixed, sha = seed(settings, 'mix.mp3'); meta = mix(settings, mixed, sha)
    items = next(b['items'] for b in client.get('/api/review/plan').json()['batches'] if b['kind']=='recognize')
    with db.transaction(settings.db_path) as conn:
        meta['recording_classification']['origin'] = 'owner'
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), mixed))
    assert enqueue(client, headers, 'recognize', items).status_code == 409
    with db.transaction(settings.db_path) as conn: conn.execute("INSERT OR REPLACE INTO settings VALUES('recovery_required','true')")
    assert not any(b['items'] for b in client.get('/api/review/plan').json()['batches'])


def test_only_current_confirmed_album_enters_group_review(client, settings):
    authenticated(client, settings); tid, sha = seed(settings)
    proposal(settings, tid, sha, {'tags': {'album': 'New album'}})
    release_id = '11111111-1111-4111-8111-111111111111'; review_id = str(uuid.uuid4())
    ref = release_review.reference({'id': release_id, 'title': 'Chosen edition', 'artist-credit': [{'name': 'Artist'}], 'media': [
        {'position': 1, 'track-count': 1, 'tracks': [{'position': 1, 'title': 'Example', 'length': 100000, 'recording': {'artist-credit': [{'name': 'Artist'}]}}]}]}, release_id)
    with db.transaction(settings.db_path) as conn:
        meta = json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()['metadata'])
        meta.update(duration=100); meta['tags'].update(tracknumber=['1'], discnumber=['1'])
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
        usage = provider_policy.rule(conn, 'musicbrainz', 'metadata'); usage.update(origin='provider_acquired', acquired_at=time.time(), expires_at=time.time()+3600)
        value = {'id': review_id, 'version': 1, 'release_id': release_id, 'reference': ref, 'usage': usage, 'reference_state': 'available',
            'members': [{'track_id': tid, 'sha256': sha}], 'expected_slots': None, 'subset_note': '', 'created_at': db.utcnow(), 'updated_at': db.utcnow()}
        conn.execute('INSERT INTO settings VALUES(?,?)', (release_review.PREFIX+review_id, db.encode(value)))
    albums = client.get('/api/review/plan').json()['albums']
    assert len(albums) == 1 and albums[0]['title'] == 'Chosen edition' and albums[0]['track_ids'] == [tid]
    with db.transaction(settings.db_path) as conn: conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('f'*64, tid))
    assert not client.get('/api/review/plan').json()['albums']
