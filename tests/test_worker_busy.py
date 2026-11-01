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

import pytest
from fastapi.testclient import TestClient

from phonoteka import db
from phonoteka import worker as worker_module
from phonoteka.app import create_app
from phonoteka.worker import Worker, sqlite_busy
from test_backend import settings, authenticated, job


def short_busy_timeout(monkeypatch):
    configure = db.configure_connection

    def configured(connection):
        configure(connection)
        # Production keeps its 15s SQLite budget; only these lock fixtures wait
        # 100ms so a failed transaction and its subsequent retry are observable.
        connection.execute('PRAGMA busy_timeout=100')
        return connection

    monkeypatch.setattr(db, 'configure_connection', configured)


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
