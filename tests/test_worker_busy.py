"""Runtime queue polling survives real SQLite writer contention without replay."""
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import threading
import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from phonoteka import db, inference_client
from phonoteka import worker as worker_module
from phonoteka.app import create_app
from phonoteka.worker import Worker, sqlite_busy
from test_backend import settings, authenticated, job, seed


def short_busy_timeout(monkeypatch):
    configure = db.configure_connection

    def configured(connection):
        configure(connection)
        # Production keeps its 15s SQLite budget; only these lock fixtures wait
        # 100ms so a failed transaction and its subsequent retry are observable.
        connection.execute('PRAGMA busy_timeout=100')
        return connection

    monkeypatch.setattr(db, 'configure_connection', configured)


def test_completed_asr_retries_local_save_without_resubmission(settings, monkeypatch):
    short_busy_timeout(monkeypatch)
    secret = settings.state_dir / 'generated-asr-key'
    secret.parent.mkdir(exist_ok=True)
    secret.write_text('generated-test-only')
    cfg = replace(settings, inference_url='http://generated-inference', inference_secret_file=secret)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        track_id, sha = seed(cfg)
        identity = job(client, owner, 'transcribe', [track_id])['id']
        submissions, saves = [], []

        class Remote:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

            async def get(self, path):
                result = {'model':'test','model_revision':'r1','profile_revision':'p1','contract':'transcribe-v3'}
                if path != '/health':
                    result = {'state':'succeeded','result':{'input_sha256':sha,'words':[],'language':'ru'}}
                return SimpleNamespace(json=lambda: result, raise_for_status=lambda: None)

            async def post(self, path, json):
                request = [json, 'test', 'r1', 'p1', 'transcribe-v3']
                remote_id = hashlib.sha256(dumps(request, sort_keys=True).encode()).hexdigest()
                submissions.append(remote_id)
                return SimpleNamespace(json=lambda: {'id': remote_id}, raise_for_status=lambda: None)

        dumps = json.dumps
        store_result = inference_client.store_result

        def contended_store(*args):
            saves.append(args[-1])
            if len(saves) == 1:
                with db.transaction(cfg.db_path):
                    return store_result(*args)
            return store_result(*args)

        monkeypatch.setattr(inference_client.httpx, 'AsyncClient', lambda **_: Remote())
        monkeypatch.setattr(inference_client, 'store_result', contended_store)
        worker = Worker(cfg)
        try:
            asyncio.run(worker.process(worker.claim_job()))
        finally:
            worker.pool.shutdown()
        assert len(submissions) == 1 and saves == submissions * 2
        assert client.get('/api/jobs/' + identity).json()['state'] == 'succeeded'
        analysis = db.fetchone(cfg.db_path, 'SELECT payload FROM analyses WHERE track_id=?', (track_id,))
        assert json.loads(analysis['payload'])['asr']['remote_job_id'] == submissions[0]
        assert len(db.fetchall(cfg.db_path, "SELECT 1 FROM events WHERE kind='analysis_completed'")) == 1


def test_busy_catalog_returns_retryable_response_without_enqueuing(settings, monkeypatch):
    short_busy_timeout(monkeypatch)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        owner = authenticated(client, settings)
        with db.transaction(settings.db_path):
            response = client.post('/api/jobs', headers={**owner, 'Idempotency-Key': 'busy-scan'}, json={'kind': 'scan'})
        assert response.status_code == 503 and response.headers['Retry-After'] == '2'
        assert response.json()['code'] == 'catalog_busy' and response.json()['retryable']
        assert not db.fetchall(settings.db_path, 'SELECT id FROM jobs')


def test_recognition_checkpoint_retries_without_repeating_search(settings, monkeypatch):
    short_busy_timeout(monkeypatch)
    cfg = replace(settings, external_fingerprints=True, file_workers=2)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        first, _ = seed(cfg, 'first.mp3')
        second, _ = seed(cfg, 'second.mp3')
        identity = job(client, owner, 'recognize', [first, second])['id']
        worker = Worker(cfg)
        seen, attempts = [], []

        async def recognize(_, item):
            seen.append(item['track_id'])
            return {'state': 'no_match'}

        original = worker.item_result

        def contended(job_id, key, state, result):
            if key == first and state == 'succeeded':
                attempts.append(state)
                if len(attempts) == 1:
                    with db.transaction(cfg.db_path):
                        return original(job_id, key, state, result)
            return original(job_id, key, state, result)

        monkeypatch.setattr(worker, 'analyze_item', recognize)
        monkeypatch.setattr(worker, 'item_result', contended)
        try:
            asyncio.run(worker.process(worker.claim_job()))
        finally:
            worker.pool.shutdown()
        result = client.get('/api/jobs/' + identity).json()
        assert result['state'] == 'succeeded' and result['completed'] == 2
        assert sorted(seen) == sorted([first, second]) and len(attempts) == 2


def test_resume_failed_recognition_admits_only_never_started_files(settings, monkeypatch):
    cfg = replace(settings, external_fingerprints=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        owner = authenticated(client, cfg)
        finished, _ = seed(cfg, 'finished.mp3')
        unknown, _ = seed(cfg, 'unknown.mp3')
        pending, _ = seed(cfg, 'pending.mp3')
        identity = job(client, owner, 'recognize', [finished, unknown, pending])['id']
        with db.transaction(cfg.db_path) as conn:
            conn.execute("UPDATE jobs SET state='failed',stage='failed',error='internal_error',completed=1 WHERE id=?", (identity,))
            conn.executemany('INSERT INTO job_items VALUES(?,?,?,?)', [
                (identity, finished, 'succeeded', db.encode({'state': 'no_match'})),
                (identity, unknown, 'running', db.encode({'checkpoint': 'provider_admitted'})),
            ])
        assert client.get('/api/jobs/' + identity).json()['resume_remaining']
        response = client.post('/api/jobs/' + identity + '/resume', headers=owner)
        assert response.status_code == 200 and response.json()['id'] == identity
        assert response.json()['state'] == 'queued' and response.json()['failed'] == 1
        assert client.post('/api/jobs/' + identity + '/resume', headers=owner).status_code == 409

        executed = []

        async def recognize(_, item):
            executed.append(item['track_id'])
            return {'state': 'no_match'}

        worker = Worker(cfg)
        monkeypatch.setattr(worker, 'analyze_item', recognize)
        try:
            asyncio.run(worker.process(worker.claim_job()))
        finally:
            worker.pool.shutdown()
        detail = client.get('/api/jobs/' + identity).json()
        by_track = {item['track_id']: item for item in detail['results']}
        assert executed == [pending] and detail['completed'] == 2 and detail['failed'] == 1
        assert by_track[unknown]['item_state'] == 'needs_review'
        assert by_track[unknown]['code'] == 'interrupted_outcome_unknown'
        assert by_track[unknown]['checkpoint'] == 'provider_admitted'
        assert by_track[finished]['item_state'] == 'succeeded'
        assert len(db.fetchall(cfg.db_path, 'SELECT id FROM jobs')) == 1


async def until(predicate, seconds=5):
    async def poll():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), seconds)


@pytest.mark.parametrize('phase', ['claim', 'schedule'])
def test_runtime_writer_lock_preserves_queue_then_executes_once(settings, monkeypatch, phase):
    short_busy_timeout(monkeypatch)
    path = settings.work_root / 'generated.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(bytes(800))
    original_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        queued = job(client, headers, 'scan')

        async def exercise():
            worker = Worker(settings)
            entered = threading.Event()
            release = threading.Event()
            in_transaction = threading.Event()
            failures = []
            main_thread = threading.get_ident()
            original = worker.claim_job if phase == 'claim' else worker_module.scheduling.tick

            def observed(*args):
                # The real SQLite transaction below must never run on the event
                # loop, including the default executor's claim polling lane.
                assert threading.get_ident() != main_thread
                if not entered.is_set():
                    entered.set()
                    assert release.wait(3), 'test controller did not release the admission barrier'
                in_transaction.set()
                try:
                    return original(*args)
                except sqlite3.OperationalError as exc:
                    if sqlite_busy(exc):
                        failures.append(exc.sqlite_errorcode)
                    raise
                finally:
                    in_transaction.clear()

            if phase == 'claim':
                monkeypatch.setattr(worker, 'claim_job', observed)
            else:
                monkeypatch.setattr(worker_module.scheduling, 'tick', observed)
            stop = asyncio.Event()

            async def stop_worker():
                await stop.wait()
                worker.stopping = True

            pulses = []

            async def responsive_loop():
                while not stop.is_set():
                    if in_transaction.is_set():
                        pulses.append(asyncio.get_running_loop().time())
                    await asyncio.sleep(.005)

            running = asyncio.create_task(worker.run())
            stopper = asyncio.create_task(stop_worker())
            ticker = asyncio.create_task(responsive_loop())
            lock = None
            try:
                await until(entered.is_set)
                lock = db.connect(settings.db_path)
                lock.execute('BEGIN IMMEDIATE')
                release.set()
                await until(lambda: failures and len(pulses) >= 3)
                # WAL readers remain usable, but the failed writer cannot have
                # started the queued operation or emitted its acknowledgement.
                state = db.fetchone(settings.db_path, 'SELECT state,completed FROM jobs WHERE id=?', (queued['id'],))
                assert state['state'] == 'queued' and state['completed'] == 0
                assert db.fetchone(settings.db_path, "SELECT count(*) AS n FROM events WHERE kind='job_started' AND json_extract(payload,'$.job_id')=?", (queued['id'],))['n'] == 0
                assert db.fetchone(settings.db_path, 'SELECT count(*) AS n FROM tracks')['n'] == 0
                assert not running.done(), 'a transient busy writer stopped the scheduler'
                lock.rollback()
                lock.close()
                lock = None
                await until(lambda: db.fetchone(settings.db_path, 'SELECT state FROM jobs WHERE id=?', (queued['id'],))['state'] == 'succeeded')
                stop.set()
                await asyncio.wait_for(running, 5)
                state = client.get('/api/jobs/' + queued['id']).json()
                assert state['completed'] == 1 and state['failed'] == 0
                assert db.fetchone(settings.db_path, "SELECT count(*) AS n FROM events WHERE kind='job_started' AND json_extract(payload,'$.job_id')=?", (queued['id'],))['n'] == 1
                assert db.fetchone(settings.db_path, 'SELECT count(*) AS n FROM tracks')['n'] == 1
                assert json.loads(db.setting(settings.db_path, 'worker_heartbeat', '{}'))['state'] == 'stopped'
            finally:
                release.set()
                if lock is not None:
                    lock.rollback()
                    lock.close()
                stop.set()
                worker.stopping = True
                await asyncio.gather(stopper, ticker)
                if not running.done():
                    await asyncio.wait_for(running, 5)
                worker.pool.shutdown(wait=True)

        asyncio.run(exercise())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_sha


def test_heartbeat_retries_real_busy_transaction_and_recovers(settings, monkeypatch):
    short_busy_timeout(monkeypatch)
    db.initialize(settings.db_path, settings=settings)

    async def exercise():
        worker = Worker(settings)
        worker.state = 'idle'
        original = worker.heartbeat
        busy = threading.Event()

        def observed():
            try:
                return original()
            except sqlite3.OperationalError as exc:
                if sqlite_busy(exc):
                    busy.set()
                raise

        monkeypatch.setattr(worker, 'heartbeat', observed)
        lock = db.connect(settings.db_path)
        lock.execute('BEGIN IMMEDIATE')
        task = asyncio.create_task(worker.heartbeats())
        try:
            await until(busy.is_set)
            assert not task.done()
            assert db.setting(settings.db_path, 'worker_heartbeat', None) is None
            lock.rollback()
            await until(lambda: db.setting(settings.db_path, 'worker_heartbeat', None) is not None)
            assert not task.done(), 'heartbeat did not remain alive after the successful retry'
            heartbeat = json.loads(db.setting(settings.db_path, 'worker_heartbeat', '{}'))
            assert heartbeat['state'] == 'idle' and heartbeat['instance_id'] == settings.instance_id
        finally:
            lock.rollback()
            lock.close()
            worker.stopping = True
            await asyncio.wait_for(task, 4)
            worker.pool.shutdown(wait=True)

    asyncio.run(exercise())


@pytest.mark.parametrize('phase', ['claim', 'heartbeat'])
def test_non_contention_operational_error_remains_fatal(settings, monkeypatch, phase):
    with TestClient(create_app(settings), base_url=settings.app_url):
        worker = Worker(settings)
        initial_heartbeat = worker.heartbeat
        calls = 0

        def invalid_query():
            with db.connect(settings.db_path) as connection:
                connection.execute('SELECT * FROM absent_queue_table')

        def heartbeat():
            nonlocal calls
            calls += 1
            if calls == 1:
                return initial_heartbeat()
            return invalid_query()

        if phase == 'claim':
            monkeypatch.setattr(worker, 'claim_job', invalid_query)
        else:
            monkeypatch.setattr(worker, 'heartbeat', heartbeat)
        try:
            with pytest.raises(sqlite3.OperationalError, match='no such table') as failure:
                asyncio.run(asyncio.wait_for(worker.run(), 5))
            assert failure.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
            assert worker.stopping
        finally:
            worker.pool.shutdown(wait=True)


@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_LOCKED_SHAREDCACHE])
def test_busy_classifier_accepts_only_contention_primary_codes(code):
    failure = sqlite3.OperationalError('synthetic extended SQLite code')
    failure.sqlite_errorcode = code
    assert sqlite_busy(failure)
    failure.sqlite_errorcode = sqlite3.SQLITE_IOERR_WRITE
    assert not sqlite_busy(failure)
    assert not sqlite_busy(sqlite3.OperationalError('database is locked'))
    assert not sqlite_busy(RuntimeError('database is locked'))


def test_main_failure_is_private_json_stdout_and_nonzero_exit(settings, monkeypatch, capsys):
    cfg = replace(settings, metrics_token_file=settings.state_dir / 'generated-scrape-token')
    worker = Worker(cfg)
    secret = 'synthetic-secret-that-must-never-reach-logs'
    private_path = '/private/collection/never-print-this-song.flac'

    async def fail_run(once=False):
        raise sqlite3.OperationalError(secret + ' ' + private_path)

    class Server:
        def __init__(self):
            self.calls = []

        def shutdown(self):
            self.calls.append('shutdown')

        def server_close(self):
            self.calls.append('close')

    server = Server()
    monkeypatch.setattr(worker, 'run', fail_run)
    monkeypatch.setattr(worker_module, 'Worker', lambda config: worker)
    monkeypatch.setattr(worker_module.Settings, 'from_env', lambda: cfg)
    monkeypatch.setattr(worker_module, 'worker_server', lambda *args: server)
    monkeypatch.setattr(worker_module.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['phonoteka-worker', '--once'])
    monkeypatch.setenv('LOG_LEVEL', 'INFO')
    # configure_logging intentionally replaces root handlers and normalizes
    # third-party loggers. Restore every touched logger after this real main()
    # invocation, including pytest's capture handlers and disabled flags.
    names = ('', 'uvicorn', 'uvicorn.error', 'uvicorn.access', 'httpx', 'httpcore',
             'faster_whisper', 'shazamio', 'asyncio')
    saved = {}
    for name in names:
        logger = logging.getLogger(name)
        saved[name] = (list(logger.handlers), logger.level, logger.propagate, logger.disabled)
    try:
        assert worker_module.main() == 1
        captured = capsys.readouterr()
        assert captured.err == ''
        assert secret not in captured.out and private_path not in captured.out
        assert str(cfg.work_root) not in captured.out
        rows = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
        assert [row['event'] for row in rows] == ['worker_starting', 'worker_failed', 'worker_stopped']
        failure = rows[1]
        assert failure['level'] == 'error' and failure['service'] == 'worker'
        assert failure['error_type'] == 'OperationalError'
        assert failure['stack'] and any(frame['function'] == 'fail_run' for frame in failure['stack'])
        assert all(set(frame) == {'module', 'function', 'line'} for frame in failure['stack'])
        assert all('/' not in frame['module'] and '\\' not in frame['module'] for frame in failure['stack'])
        assert server.calls == ['shutdown', 'close']
    finally:
        for handler in logging.getLogger().handlers:
            if handler not in saved[''][0]:
                handler.close()
        for name, (handlers, level, propagate, disabled) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
        worker.pool.shutdown(wait=True)
