"""Real SQLite writer contention must not invalidate a verified media export."""
import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from phonoteka import db, exports
from phonoteka.app import create_app
from test_backend import settings, authenticated, job, drain
from test_exports import published
from test_worker_busy import short_busy_timeout


@pytest.mark.parametrize('blocked_sample', [0, 1, 2])
def test_busy_export_counter_does_not_abort_snapshot(settings, monkeypatch, caplog, blocked_sample):
    short_busy_timeout(monkeypatch)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        first = published(settings, 'Artist/one.mp3')
        second = published(settings, 'Second Artist/two.mp3')
        queued = job(client, headers, 'export_ready')
        generate = exports.generate
        samples = []

        def contended(config, authorize, progress, **kwargs):
            def report(copied, total):
                samples.append(copied)
                if copied != blocked_sample:
                    progress(copied, total)
                    return
                # This produces an actual SQLITE_BUSY at BEGIN IMMEDIATE, not
                # a fabricated exception or a replacement media-copy result.
                with db.connect(config.db_path) as lock:
                    lock.execute('BEGIN IMMEDIATE')
                    try:
                        progress(copied, total)
                    finally:
                        lock.rollback()
            return generate(config, authorize, report, **kwargs)

        monkeypatch.setattr(exports, 'generate', contended)
        # Ensure the intermediate callback is sampled even for tiny fixtures.
        import phonoteka.worker as worker_module
        real_time = worker_module.time
        class Clock:
            ticks = 0
            def monotonic(self):
                self.ticks += 2
                return real_time.monotonic() + self.ticks
            def __getattr__(self, name):
                return getattr(real_time, name)
        monkeypatch.setattr(worker_module, 'time', Clock())
        drain(settings)
        result = client.get('/api/jobs/' + queued['id']).json()
        assert result['state'] == 'succeeded' and result['completed'] == 1 and result['failed'] == 0
        assert samples == [0, 1, 2]
        assert any(getattr(record, 'event', '') == 'worker_database_busy' and
                   getattr(record, 'fields', {}).get('operation') == 'export_progress' for record in caplog.records)
        document = client.get('/api/exports/latest').json()['export']
        assert document['audio_files'] == 2
        assert {entry['sha256'] for entry in document['files']} == {first[1], second[1]}
        for entry in document['files']:
            snapshot = settings.state_dir / 'exports' / document['id'] / 'files' / entry['path']
            assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == entry['sha256']
        assert hashlib.sha256(first[2].read_bytes()).hexdigest() == first[1]
        assert hashlib.sha256(second[2].read_bytes()).hexdigest() == second[1]
        assert len(db.fetchall(settings.db_path, "SELECT * FROM job_items WHERE job_id=?", (queued['id'],))) == 1


def test_non_contention_export_counter_error_preserves_previous_snapshot(settings, monkeypatch):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        _, sha, ready = published(settings)
        previous = exports.generate(settings)['export_id']
        queued = job(client, headers, 'export_ready')
        generate = exports.generate
        transaction = db.transaction

        def invalid_schema(config, authorize, progress, **kwargs):
            def report(copied, total):
                if copied:
                    progress(copied, total)
                    return
                def fail(_path):
                    with sqlite3.connect(':memory:') as connection:
                        connection.execute('SELECT * FROM nonexistent_table')
                monkeypatch.setattr(db, 'transaction', fail)
                try:
                    progress(copied, total)
                finally:
                    monkeypatch.setattr(db, 'transaction', transaction)
            return generate(config, authorize, report, **kwargs)

        monkeypatch.setattr(exports, 'generate', invalid_schema)
        drain(settings)
        result = client.get('/api/jobs/' + queued['id']).json()
        assert result['state'] == 'failed' and result['failed'] == 1
        assert result['error'] == 'internal_error'
        assert db.setting(settings.db_path, 'latest_export') == previous
        assert hashlib.sha256(ready.read_bytes()).hexdigest() == sha
        assert not list((settings.state_dir / 'exports').glob('.stage-*'))
