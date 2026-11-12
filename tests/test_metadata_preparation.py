"""Native scoped observations, durable orchestration and fail-closed preparation."""
import asyncio
from dataclasses import replace
import json
import time
import uuid

import pytest

from phonoteka import db, input_readiness, media, metadata_preparation as prep, provider_attempts
from phonoteka.worker import Worker
from test_backend import drain, settings as base_settings
from test_confirmed_fields import library


@pytest.fixture
def settings(tmp_path):
    return replace(base_settings.__wrapped__(tmp_path), external_metadata=True, external_fingerprints=True, file_workers=1)


def entry(client):
    return next(i for i in client.get('/api/confirmed-fields/plan').json()['items'] if i['relative_path'] == 'sample.flac')


def setup(cfg, tid, *, unstable=True, stale=True):
    with db.transaction(cfg.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        meta = json.loads(row['metadata'])
        if unstable:
            meta['input_readiness'] = input_readiness.observed(meta, {}, None)
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
        if stale:
            conn.execute('UPDATE analyses SET input_sha256=? WHERE track_id=?', ('0'*64, tid))


def start(client, headers, key=None):
    value = entry(client)
    assert value['preparation'], value
    return client.post('/api/metadata-preparation', json={'tokens': {value['track_id']: value['preparation']['token']}},
                       headers={**headers, 'Idempotency-Key': key or str(uuid.uuid4())})


def test_two_real_scans_keep_other_rows_and_files_untouched_and_queue_search(library):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid)
    with db.transaction(cfg.db_path) as conn:
        other = dict(conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone())
        other['id'] = str(uuid.uuid4()); other['relative_path'] = 'unseen.flac'
        conn.execute('INSERT INTO tracks(' + ','.join(other) + ') VALUES(' + ','.join('?' for _ in other) + ')', tuple(other.values()))
    before = media.sha256_file(path)
    response = start(client, headers)
    assert response.status_code == 202, response.text
    first = response.json(); assert first['stage'] == 'scan_first'
    drain(cfg)
    prep.tick(cfg)
    second = client.get('/api/metadata-preparation').json()
    assert second['stage'] == 'scan_second' and second['job_id'] != first['job_id']
    drain(cfg)
    prep.tick(cfg)
    third = client.get('/api/metadata-preparation').json()
    assert third['stage'] == 'search'
    with db.connect(cfg.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()
        assert input_readiness.describe(json.loads(row[0]), grace_seconds=0)['state'] == 'ready'
        assert dict(conn.execute('SELECT * FROM tracks WHERE id=?', (other['id'],)).fetchone()) == other
        assert conn.execute('SELECT kind FROM jobs WHERE id=?', (third['job_id'],)).fetchone()[0] == 'recognize'
    assert media.sha256_file(path) == before == sha
    assert client.post('/api/metadata-preparation/cancel', json={'id': client.get('/api/metadata-preparation').json()['id']}, headers=headers).status_code == 200


def test_idempotent_replay_after_scan_and_cancel_does_not_spend_again(library):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid)
    original = entry(client); body = {'tokens': {tid: original['preparation']['token']}}
    key = str(uuid.uuid4()); h = {**headers, 'Idempotency-Key': key}
    first = client.post('/api/metadata-preparation', json=body, headers=h)
    assert first.status_code == 202
    drain(cfg)
    assert client.post('/api/metadata-preparation', json=body, headers=h).json()['id'] == key
    stopped = client.post('/api/metadata-preparation/cancel', json={'id': client.get('/api/metadata-preparation').json()['id']}, headers=headers).json()
    assert stopped['state'] == 'stopped'
    prep.tick(cfg)
    assert client.post('/api/metadata-preparation', json=body, headers=h).json()['state'] == 'stopped'
    assert entry(client)['preparation'] is None
    with db.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE json_extract(payload,'$.trigger')=?", (prep.CONTRACT,)).fetchone()[0] == 1


@pytest.mark.parametrize('change', ['revision', 'choice', 'held', 'epoch', 'root'])
def test_dependency_change_stops_before_second_scan(library, change, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid)
    assert start(client, headers).status_code == 202
    drain(cfg)
    with db.transaction(cfg.db_path) as conn:
        if change == 'revision': conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('f'*64, tid))
        if change == 'choice':
            conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (str(uuid.uuid4()), tid, sha, db.encode({'tags': {'album': 'Disputed'}}), db.utcnow()))
        if change == 'held':
            stamp = db.utcnow()
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,'recognize','needs_review','held',?,1,?,?,1)",
                         (str(uuid.uuid4()), db.encode({'items': [{'track_id': tid}]}), stamp, stamp))
        if change == 'epoch': conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
    if change == 'root': monkeypatch.setattr('phonoteka.worker.root_identity', lambda _: {'changed': True})
    prep.tick(cfg)
    with db.connect(cfg.db_path) as conn:
        assert prep.load(conn)['state'] == 'stopped'
        assert conn.execute("SELECT count(*) FROM jobs WHERE json_extract(payload,'$.trigger')=?", (prep.CONTRACT,)).fetchone()[0] == 1
    if change == 'epoch': assert client.get('/api/metadata-preparation').status_code == 401


def test_partial_scan_never_advances_or_confirms_and_changed_file_is_not_replaced(library, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid)
    run = start(client, headers).json()
    # A physical replacement after the frozen plan cannot create a successor ID.
    with path.open('ab') as f: f.write(b'external edit')
    drain(cfg); prep.tick(cfg)
    assert client.get('/api/metadata-preparation').json()['state'] == 'stopped'
    with db.connect(cfg.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM tracks WHERE is_current=1').fetchone()[0] == 1
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        assert row['sha256'] == sha and json.loads(row['metadata'])['input_readiness']['confirmations'] == 0
        assert conn.execute('SELECT state FROM jobs WHERE id=?', (run['job_id'],)).fetchone()[0] == 'completed_with_errors'


def test_successful_search_has_durable_receipt_and_does_not_write_or_accept(library, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid, unstable=False)
    async def search(self, job, item):
        with db.transaction(cfg.db_path) as conn:
            conn.execute('UPDATE analyses SET input_sha256=?,payload=?,updated_at=? WHERE track_id=?',
                         (sha, db.encode({'candidates': [], 'sources': [], 'state': 'no_match'}), db.utcnow(), tid))
        return {'state': 'no_match'}
    monkeypatch.setattr(Worker, 'analyze_item', search)
    started = start(client, headers).json()
    assert started['stage'] == 'search'
    drain(cfg); prep.tick(cfg)
    assert client.get('/api/metadata-preparation').json()['state'] == 'review'
    assert entry(client)['preparation'] is None
    assert not entry(client)['ready'] and media.sha256_file(path) == sha
    with db.connect(cfg.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM proposals').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind IN ('apply','publish')").fetchone()[0] == 0


def test_forged_marker_and_stale_plan_fail_closed(library):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid)
    value = entry(client)
    body = {'tokens': {tid: value['preparation']['token']}}
    setup(cfg, tid)
    assert client.post('/api/metadata-preparation', json=body, headers={**headers, 'Idempotency-Key': str(uuid.uuid4())}).status_code == 409
    fake = {'id': str(uuid.uuid4()), 'payload': db.encode({'trigger': prep.CONTRACT, 'metadata_preparation_id': str(uuid.uuid4()), 'items': []}), 'kind': 'scan', 'auth_epoch': 1}
    with pytest.raises(media.MediaError): prep.authorize(cfg, fake)
    assert client.post('/api/metadata-preparation', json={'tokens': {str(i): 'x' for i in range(51)}}, headers={**headers, 'Idempotency-Key': str(uuid.uuid4())}).status_code == 422


def test_stale_cancel_cannot_stop_current_run_and_initial_root_denial_spends_nothing(library):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid)
    stored = db.setting(cfg.db_path, 'work_root_identity')
    db.set_setting(cfg.db_path, 'work_root_identity', db.encode({'changed': True}))
    denied = start(client, headers)
    assert denied.status_code == 409
    with db.connect(cfg.db_path) as conn:
        assert not prep.load(conn)
        assert conn.execute("SELECT count(*) FROM settings WHERE key LIKE 'metadata_prepared:%'").fetchone()[0] == 0
    db.set_setting(cfg.db_path, 'work_root_identity', stored)
    run = start(client, headers).json()
    wrong = client.post('/api/metadata-preparation/cancel', json={'id': str(uuid.uuid4())}, headers=headers)
    assert wrong.status_code == 409 and client.get('/api/metadata-preparation').json()['state'] == 'active'
    assert client.post('/api/metadata-preparation/cancel', json={'id': run['id']}, headers=headers).status_code == 200


def test_native_recognition_discovered_mix_does_not_poison_other_records(library, monkeypatch):
    import shutil
    from phonoteka import recognition
    from phonoteka.providers import Providers
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid, unstable=False)
    second = str(uuid.uuid4())
    with db.transaction(cfg.db_path) as conn:
        row = dict(conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone())
        row.update(id=second, relative_path='second.flac')
        conn.execute('INSERT INTO tracks('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')', tuple(row.values()))
    shutil.copy2(path, cfg.work_root/'second.flac')
    calls = []
    async def identify(providers, sample, duration, authorized, **kwargs):
        authorized(); calls.append('fingerprint')
        mixed = len(calls) == 1
        return {'segments': [], 'state': 'conflict' if mixed else 'no_match', 'possible_mix': mixed}
    async def analyze(self, meta, relative, identified, authorized, **kwargs):
        authorized()
        return {'input_sha256': sha, 'state': 'no_match', 'candidates': [], 'sources': []}
    monkeypatch.setattr(recognition, 'identify_segments', identify)
    monkeypatch.setattr(Providers, 'analyze', analyze)
    values = client.get('/api/confirmed-fields/plan').json()['items']
    body = {'tokens': {i['track_id']: i['preparation']['token'] for i in values}}
    started = client.post('/api/metadata-preparation', json=body, headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
    assert started.status_code == 202, started.text
    drain(cfg); prep.tick(cfg)
    assert len(calls) == 2 and client.get('/api/metadata-preparation').json()['state'] == 'review'
    with db.connect(cfg.db_path) as conn:
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (started.json()['job_id'],)).fetchone()
        assert job['completed'] == 2 and job['failed'] == 0
        flags = [json.loads(r[0]).get('possible_mix') for r in conn.execute('SELECT metadata FROM tracks WHERE is_current=1')]
        assert sorted(flags) == [False, True]
        assert conn.execute('SELECT count(*) FROM proposals').fetchone()[0] == 0
    assert media.sha256_file(path) == media.sha256_file(cfg.work_root/'second.flac') == sha


def test_complete_observations_do_not_skip_grace_period(library):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid)
    assert start(client, headers).status_code == 202
    waiting = replace(cfg, input_grace_seconds=3600)
    drain(waiting); prep.tick(waiting)
    drain(waiting); prep.tick(waiting)
    value = client.get('/api/metadata-preparation').json()
    assert value['state'] == 'active' and value['reason'] == 'waiting_input_grace'
    assert value['stage'] == 'search' and not value['job_id']
    with db.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind='recognize'").fetchone()[0] == 0


@pytest.mark.parametrize('code', ['recognition_attempt_budget_exhausted', 'audit_failed'])
def test_expected_budget_limit_is_pending_and_genuine_failure_still_stops(library, monkeypatch, code):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid, unstable=False)
    with db.transaction(cfg.db_path) as conn:
        meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
        identity=meta['audit']['audio_identity']
        conn.execute('INSERT INTO settings VALUES(?,?)', ('recognition_usage:'+identity, db.encode({'calls':3})))
    value=entry(client)
    assert value['preparation']['fingerprint_requests_remaining']==0
    assert 'Новые запросы' in value['preparation']['reason']
    async def search(self, job, item):
        raise media.MediaError(code, 'synthetic deterministic source outcome')
    monkeypatch.setattr(Worker,'analyze_item',search)
    run=start(client,headers).json()
    drain(cfg);prep.tick(cfg)
    result=client.get('/api/metadata-preparation').json()
    assert result['state']==('review' if code=='recognition_attempt_budget_exhausted' else 'stopped')
    with db.connect(cfg.db_path) as conn:
        usage=json.loads(conn.execute('SELECT value FROM settings WHERE key=?',('recognition_usage:'+identity,)).fetchone()[0])
        assert usage['calls']==3
        row=conn.execute('SELECT state,result FROM job_items WHERE job_id=?',(run['job_id'],)).fetchone()
        if code=='recognition_attempt_budget_exhausted':
            assert row['state']=='succeeded'
            assert json.loads(row['result'])['state']=='needs_review'
            assert result['progress']['skipped']==1 and result['progress']['failed']==0
            detail = client.get('/api/jobs/'+run['job_id']).json()
            assert detail['skipped'] == detail['completed'] == 1 and detail['failed'] == 0
            listed = next(j for j in client.get('/api/jobs').json()['items'] if j['id'] == run['job_id'])
            assert listed['skipped'] == 1
        else:assert row['state']=='failed'
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind IN ('apply','publish')").fetchone()[0]==0
    assert media.sha256_file(path)==sha and entry(client)['preparation'] is None


def test_changed_capacity_invalidates_frozen_ui_plan_without_spending(library):
    cfg,client,headers,tid,sha,path=library
    setup(cfg,tid,unstable=False)
    value=entry(client)
    assert value['preparation']['fingerprint_requests_remaining']==3
    with db.transaction(cfg.db_path) as conn:
        meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
        conn.execute('INSERT INTO settings VALUES(?,?)',('recognition_usage:'+meta['audit']['audio_identity'],db.encode({'calls':3})))
    response=client.post('/api/metadata-preparation',json={'tokens':{tid:value['preparation']['token']}},headers={**headers,'Idempotency-Key':str(uuid.uuid4())})
    assert response.status_code==409
    with db.connect(cfg.db_path) as conn:
        assert not prep.load(conn)
        assert conn.execute("SELECT count(*) FROM settings WHERE key LIKE 'metadata_prepared:%'").fetchone()[0]==0


@pytest.mark.parametrize('calls', [2, 3])
def test_new_cache_capability_preserves_legacy_receipt_and_send_allowance(library, calls):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid, unstable=False)
    with db.transaction(cfg.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        meta = json.loads(row['metadata'])
        usage_key = provider_attempts.RECOGNITION_PREFIX + meta['audit']['audio_identity']
        conn.execute('INSERT INTO settings VALUES(?,?)', (usage_key, db.encode({'calls': calls})))
        legacy = prep.fields.digest([prep.CONTRACT, tid, sha, 'recognize', prep.guards(conn, row)])
        conn.execute('INSERT INTO settings VALUES(?,?)', ('metadata_prepared:' + legacy, db.encode({'run_id': 'legacy-retained'})))
    current = entry(client)
    assert bool(current['preparation']) == (calls == 3)
    if calls == 3:
        assert current['preparation']['fingerprint_requests_remaining'] == 0
        started = start(client, headers).json()
        assert client.post('/api/metadata-preparation/cancel', json={'id': started['id']}, headers=headers).status_code == 200
        assert entry(client)['preparation'] is None
        with db.transaction(cfg.db_path) as conn:
            conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
            row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
            result = prep.fields.evaluate(cfg, conn, row, {'auth_epoch': 2}, busy=set(), retired=set())
            assert prep.candidate(cfg, conn, row, {'auth_epoch': 2}, result) is None
    with db.connect(cfg.db_path) as conn:
        assert json.loads(conn.execute('SELECT value FROM settings WHERE key=?', (usage_key,)).fetchone()[0]) == {'calls': calls}
        assert json.loads(conn.execute('SELECT value FROM settings WHERE key=?', ('metadata_prepared:' + legacy,)).fetchone()[0]) == {'run_id': 'legacy-retained'}
        assert conn.execute("SELECT count(*) FROM settings WHERE key LIKE 'metadata_prepared:%'").fetchone()[0] == (2 if calls == 3 else 1)
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind IN ('apply','publish')").fetchone()[0] == 0
    assert media.sha256_file(path) == sha


def test_new_cache_capability_does_not_replay_metadata_receipts(library):
    cfg, client, headers, tid, sha, path = library
    setup(cfg, tid, unstable=False, stale=False)
    with db.transaction(cfg.db_path) as conn:
        analysis = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()[0])
        analysis['candidates'] = [c for c in analysis['candidates'] if c['provider'] == 'acoustid']
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(analysis), tid))
    assert entry(client)['preparation']['kind'] == 'analyze'
    with db.transaction(cfg.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        identity = json.loads(row['metadata'])['audit']['audio_identity']
        conn.execute('INSERT INTO settings VALUES(?,?)', (provider_attempts.RECOGNITION_PREFIX + identity, db.encode({'calls': 3})))
        legacy = prep.fields.digest([prep.CONTRACT, tid, sha, 'analyze', prep.guards(conn, row)])
        conn.execute('INSERT INTO settings VALUES(?,?)', ('metadata_prepared:' + legacy, db.encode({'run_id': 'legacy-retained'})))
    assert entry(client)['preparation'] is None


@pytest.mark.parametrize('kind,calls,skipped', [('recognize',3,True),('recognize',2,False),('analyze',3,False)])
def test_native_returned_retry_respects_remaining_fingerprint_capacity_only(library, monkeypatch, kind, calls, skipped):
    cfg,client,headers,tid,sha,path=library
    setup(cfg,tid,unstable=False,stale=kind=='recognize')
    if kind=='analyze':
        with db.transaction(cfg.db_path) as conn:
            analysis=json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?',(tid,)).fetchone()[0])
            analysis['candidates']=[c for c in analysis['candidates'] if c['provider']=='acoustid']
            conn.execute('UPDATE analyses SET payload=? WHERE track_id=?',(db.encode(analysis),tid))
    assert entry(client)['preparation']['kind']==kind
    saved={'input_sha256':sha,'state':'no_match','candidates':[], 'sources':[
        {'provider':provider,'state':'provider_unavailable' if provider==('shazam' if kind=='recognize' else 'musicbrainz') else 'no_match'}
        for provider in ('shazam','acoustid','musicbrainz','lrclib')]}
    due=time.time()+3600
    native={'state':'no_match','candidates':0,'sources':saved['sources'],'retry_at':due}
    async def searched(self,job,item):
        with db.transaction(cfg.db_path) as conn:
            meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                         (provider_attempts.RECOGNITION_PREFIX+meta['audit']['audio_identity'],db.encode({'calls':calls})))
            conn.execute('UPDATE analyses SET input_sha256=?,payload=?,updated_at=? WHERE track_id=?',(sha,db.encode(saved),db.utcnow(),tid))
        return native
    monkeypatch.setattr(Worker,'analyze_item',searched)
    accepted=start(client,headers).json()
    drain(cfg);prep.tick(cfg)
    with db.connect(cfg.db_path) as conn:
        row=conn.execute('SELECT state,result FROM job_items WHERE job_id=? AND item_key=?',(accepted['job_id'],tid)).fetchone()
        result=json.loads(row['result'])
        assert json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?',(tid,)).fetchone()[0])==saved
        meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
        assert json.loads(conn.execute('SELECT value FROM settings WHERE key=?',(provider_attempts.RECOGNITION_PREFIX+meta['audit']['audio_identity'],)).fetchone()[0])['calls']==calls
        child=conn.execute('SELECT state,stage FROM jobs WHERE id=?',(accepted['job_id'],)).fetchone()
        if skipped:
            assert row['state']=='succeeded' and result['skipped'] is True
            assert result['code']=='recognition_attempt_budget_exhausted' and 'retry_at' not in result
            assert child['state']=='succeeded' and prep.load(conn)['state']=='review'
        else:
            assert row['state']=='deferred' and result==native
            assert child['state']=='queued' and child['stage']=='waiting_provider' and prep.load(conn)['state']=='active'
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind IN ('apply','publish')").fetchone()[0]==0
    assert media.sha256_file(path)==sha


@pytest.mark.parametrize('change', ['revision','missing','audit_state','audit_sha','audit_profile','identity','grant'])
def test_returned_retry_does_not_mask_changed_input_audit_or_grant(library, monkeypatch, change):
    cfg,client,headers,tid,sha,path=library
    setup(cfg,tid,unstable=False)
    accepted=start(client,headers).json()
    job=dict(db.fetchone(cfg.db_path,'SELECT * FROM jobs WHERE id=?',(accepted['job_id'],)))
    item=json.loads(job['payload'])['items'][0]
    async def searched(self,job,item):
        with db.transaction(cfg.db_path) as conn:
            meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
            identity=meta['audit']['audio_identity']
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(provider_attempts.RECOGNITION_PREFIX+identity,db.encode({'calls':3})))
            if change=='revision':conn.execute('UPDATE tracks SET sha256=? WHERE id=?',('f'*64,tid))
            if change=='missing':conn.execute('UPDATE tracks SET is_current=0 WHERE id=?',(tid,))
            if change.startswith('audit_') or change=='identity':
                field={'audit_state':'state','audit_sha':'sha256','audit_profile':'profile','identity':'audio_identity'}[change]
                meta['audit'][field]={'audit_state':'failed','audit_sha':'0'*64,'audit_profile':'partial','identity':'bad'}[change]
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),tid))
            if change=='grant':conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
        return {'state':'no_match','retry_at':time.time()+3600}
    monkeypatch.setattr(Worker,'analyze_item',searched)
    worker=Worker(cfg)
    try:
        with pytest.raises(media.MediaError) as error:asyncio.run(prep.search(worker,job,item))
        assert error.value.code in {'conflict','preparation_stale','audit_failed','authorization_changed','authorization_epoch_changed'}
    finally:worker.pool.shutdown(wait=True)
    assert media.sha256_file(path)==sha


def test_returned_retry_preserves_other_capacity_error(library,monkeypatch):
    cfg,client,headers,tid,sha,path=library
    setup(cfg,tid,unstable=False)
    accepted=start(client,headers).json()
    job=dict(db.fetchone(cfg.db_path,'SELECT * FROM jobs WHERE id=?',(accepted['job_id'],)))
    item=json.loads(job['payload'])['items'][0]
    async def searched(self,job,item):return {'retry_at':time.time()+3600}
    def invalid(*_):raise provider_attempts.UsageError('recognition_identity_required')
    monkeypatch.setattr(Worker,'analyze_item',searched)
    monkeypatch.setattr(provider_attempts,'require_recognition_capacity',invalid)
    worker=Worker(cfg)
    try:
        with pytest.raises(provider_attempts.UsageError) as error:asyncio.run(prep.search(worker,job,item))
        assert error.value.code=='recognition_identity_required'
    finally:worker.pool.shutdown(wait=True)


@pytest.mark.parametrize('change',['grant','revision','audit'])
def test_budget_decision_rechecks_input_and_grant_after_capacity_read(library,monkeypatch,change):
    cfg,client,headers,tid,sha,path=library
    setup(cfg,tid,unstable=False)
    accepted=start(client,headers).json()
    job=dict(db.fetchone(cfg.db_path,'SELECT * FROM jobs WHERE id=?',(accepted['job_id'],)))
    item=json.loads(job['payload'])['items'][0]
    async def searched(self,job,item):return {'retry_at':time.time()+3600}
    def changed(settings,identity):
        with db.transaction(cfg.db_path) as conn:
            if change=='grant':conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
            if change=='revision':conn.execute('UPDATE tracks SET sha256=? WHERE id=?',('0'*64,tid))
            if change=='audit':
                meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
                meta['audit']['state']='failed'
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),tid))
        raise provider_attempts.UsageError('recognition_attempt_budget_exhausted')
    monkeypatch.setattr(Worker,'analyze_item',searched)
    monkeypatch.setattr(provider_attempts,'require_recognition_capacity',changed)
    worker=Worker(cfg)
    try:
        with pytest.raises(media.MediaError):asyncio.run(prep.search(worker,job,item))
    finally:worker.pool.shutdown(wait=True)
