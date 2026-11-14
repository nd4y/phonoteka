"""Idle attempt-ledger recovery must keep deadlines and spent calls intact."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3

import pytest

from phonoteka import db, provider_attempts as attempts
from phonoteka.worker import Worker
from test_backend import client, settings


def put(conn, key, **fields):
    value = {'state': 'succeeded', 'reserved_at': 1800000000, 'finished_at': 1800000000,
             'budget_spent': True, **fields}
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('provider_attempt:' + key, db.encode(value)))


def reads():
    return int(next(line.split()[1] for line in Path('/proc/self/io').read_text().splitlines() if line.startswith('rchar:')))


def test_unchanged_attempt_history_does_not_repeat_sweep(client, settings):
    now = 1800000010
    with db.transaction(settings.db_path) as conn:
        for index in range(5000):
            put(conn, str(index), padding='x' * 1024)
    attempts.recover_stale(settings, now=now)
    before = reads()
    for delta in (30, 60, 90):
        attempts.recover_stale(settings, now=now + delta)
    measured = reads() - before
    report = {'terminal_records': 5000, 'idle_sweeps': 3, 'logical_read_bytes': measured}
    target = os.environ.get('PHONOTEKA_ATTEMPT_BENCHMARK_REPORT')
    if target:
        Path(target).write_text(json.dumps(report), encoding='utf-8')
    assert measured < 1024**2, report
    assert db.setting(settings.db_path, 'provider_attempt_sweep_at') == str(now)
    assert db.fetchone(settings.db_path, "SELECT COUNT(*) n FROM settings WHERE key LIKE 'provider_attempt:%'")['n'] == 5000


def test_attempt_sweep_preserves_strict_stale_and_retention_boundaries(client, settings):
    now = 1800000000
    with db.transaction(settings.db_path) as conn:
        put(conn, 'pending', state='sent', reserved_at=now-attempts.STALE_SECONDS+.5)
        put(conn, 'old', finished_at=now-attempts.RETENTION_SECONDS+.5)
        put(conn, 'lost', state='reserved', reserved_at=now-attempts.STALE_SECONDS-1)
        conn.execute('INSERT INTO settings VALUES(?,?)', ('provider_attempt:no-finish', db.encode({'state': 'unknown_outcome', 'budget_spent': True})))
    attempts.recover_stale(settings, now=now)
    assert json.loads(db.setting(settings.db_path, 'provider_attempt:lost'))['state'] == 'unknown_outcome'
    attempts.recover_stale(settings, now=now+.5)
    assert json.loads(db.setting(settings.db_path, 'provider_attempt:pending'))['state'] == 'sent'
    assert db.setting(settings.db_path, 'provider_attempt:old') is not None
    attempts.recover_stale(settings, now=now+.51)
    pending = json.loads(db.setting(settings.db_path, 'provider_attempt:pending'))
    assert pending['state'] == 'unknown_outcome' and pending['budget_spent'] is True
    assert db.setting(settings.db_path, 'provider_attempt:old') is None
    assert db.setting(settings.db_path, 'provider_attempt:no-finish') is not None


def test_attempt_schedule_survives_restart_and_legacy_edits_and_rolls_back(client, settings):
    now = 1800000010
    with db.transaction(settings.db_path) as conn:
        put(conn, 'legacy')
    attempts.recover_stale(settings, now=now)
    original = db.fetchone(settings.db_path, 'SELECT due FROM attempt_sweep_schedule')['due']
    db.initialize(settings.db_path)
    assert db.fetchone(settings.db_path, 'SELECT due FROM attempt_sweep_schedule')['due'] == original
    with pytest.raises(RuntimeError, match='rollback'):
        with db.transaction(settings.db_path) as conn:
            put(conn, 'legacy', state='sent')
            assert conn.execute('SELECT due FROM attempt_sweep_schedule').fetchone()[0] == 0
            raise RuntimeError('rollback')
    assert db.fetchone(settings.db_path, 'SELECT due FROM attempt_sweep_schedule')['due'] == original
    with sqlite3.connect(settings.db_path) as conn:
        conn.create_function('phonoteka_schema_version', 0, lambda: 2)
        conn.execute("UPDATE settings SET value=? WHERE key='provider_attempt:legacy'", (db.encode({'state': 'sent', 'reserved_at': now-181, 'budget_spent': True}),))
    attempts.recover_stale(settings, now=now+1)
    assert json.loads(db.setting(settings.db_path, 'provider_attempt:legacy'))['state'] == 'unknown_outcome'
    with db.transaction(settings.db_path) as conn:
        conn.execute("DELETE FROM settings WHERE key='provider_attempt:legacy'")
    attempts.recover_stale(settings, now=now+2)
    assert db.fetchone(settings.db_path, 'SELECT due FROM attempt_sweep_schedule')['due'] is None


@pytest.mark.parametrize('raw,error', [('{broken', ValueError), ('{}', KeyError), ('{"state":"succeeded","finished_at":null}', TypeError)])
def test_invalid_attempt_edits_do_not_silently_park_or_refund(client, settings, raw, error):
    attempts.recover_stale(settings, now=1800000000)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO settings VALUES(?,?)', ('provider_attempt:invalid', raw))
    with pytest.raises(error):
        attempts.recover_stale(settings, now=1800000001)


def test_attempt_admission_throttle_stays_but_forced_dirty_recovery_runs(client, settings):
    now = 1800000000
    attempts.recover_stale(settings, now=now)
    with db.transaction(settings.db_path) as conn:
        put(conn, 'pending', state='reserved', reserved_at=now-181)
        attempts._maintain(conn, now+1)
    assert json.loads(db.setting(settings.db_path, 'provider_attempt:pending'))['state'] == 'reserved'
    attempts.recover_stale(settings, now=now+1)
    assert json.loads(db.setting(settings.db_path, 'provider_attempt:pending'))['state'] == 'unknown_outcome'


def test_idle_claims_keep_one_wal_lifecycle_until_worker_stops(settings):
    worker = Worker(settings)
    claim = worker.claim_job
    wal = Path(str(settings.db_path) + '-wal')
    samples = []

    def inode():
        try:return wal.stat().st_ino
        except FileNotFoundError:return None

    def observe():
        before = inode()
        result = claim()
        samples.append((before, inode()))
        if len(samples) == 3:worker.stopping = True
        return result

    worker.claim_job = observe
    asyncio.run(worker.run())
    assert len(samples) == 3
    assert all(before is not None and before == after == samples[0][0] for before, after in samples)
    assert not wal.exists(), 'The anchor must close when the worker exits'
