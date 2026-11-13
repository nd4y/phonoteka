"""Idle paths must not materialize historical payloads or skip expiry enforcement."""
import json
import sqlite3
import time

import pytest

from phonoteka import db, provider_policy as policy
from phonoteka.app import public_job
from phonoteka.metrics import DatabaseCollector
from test_backend import authenticated, client, settings, seed
from test_provider_policy import grant, lyrics


def insert_job(conn, identity, payload, *, kind='scan', state='succeeded', stage=None):
    conn.execute('INSERT INTO jobs(id,kind,state,stage,payload,total,completed,failed,created_at,updated_at) VALUES(?,?,?,?,?,3,2,1,?,?)',
                 (identity, kind, state, stage or state, db.encode(payload), db.utcnow(), db.utcnow()))


def test_list_status_matches_full_rows_without_reading_frozen_payload(client, settings, monkeypatch):
    authenticated(client, settings)
    with db.transaction(settings.db_path) as conn:
        for index, version in enumerate((0, 7, True, 'bad')):
            insert_job(conn, f'list-{index}', {'items': [{'large': 'x' * 200000}],
                'trigger': 'metadata-preparation-v1', 'params': {'reconcile_root': True},
                'pause_requested': index == 1, 'authorization_version': version,
                'authorization_wait': {'reason': 'authorization_revoked'}, 'export_progress': {'completed': 2}},
                kind='export_ready' if index == 0 else 'scan', state='waiting_authorization' if index == 2 else 'succeeded')
    expected = {r['id']: public_job(settings, r) for r in db.fetchall(settings.db_path, 'SELECT * FROM jobs')}
    assert expected['list-2']['authorization']['reason'] == 'authorization_revoked'
    assert expected['list-2']['authorization']['version'] == 0
    original = db.connect

    def light_connection(*args, **kwargs):
        conn = original(*args, **kwargs)
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == 'jobs' and column == 'payload':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(db, 'connect', light_connection)
    response = client.get('/api/jobs?active_first=true&recoverable_first=true')
    assert response.status_code == 200, response.text
    assert {r['id']: r for r in response.json()['items']} == expected
    assert response.json()['total'] == 4


def test_status_projection_changes_atomically_and_cannot_bypass_schema_guard(client, settings):
    with db.transaction(settings.db_path) as conn:
        insert_job(conn, 'atomic', {'pause_requested': False})
    with pytest.raises(RuntimeError):
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE jobs SET payload=? WHERE id=?', (db.encode({'pause_requested': True}), 'atomic'))
            raise RuntimeError('rollback')
    assert not json.loads(db.fetchone(settings.db_path, 'SELECT public_payload FROM job_status WHERE job_id=?', ('atomic',))['public_payload'])['pause_requested']
    with db.transaction(settings.db_path) as conn:
        conn.create_function('phonoteka_schema_version', 0, lambda: 1)
        with pytest.raises(sqlite3.IntegrityError, match='schema_write_guard'):
            conn.execute("DELETE FROM policy_cleanup")
        conn.create_function('phonoteka_schema_version', 0, lambda: 2)
        conn.execute('UPDATE jobs SET payload=? WHERE id=?', (db.encode({'pause_requested': True}), 'atomic'))
    assert json.loads(db.fetchone(settings.db_path, 'SELECT public_payload FROM job_status WHERE job_id=?', ('atomic',))['public_payload'])['pause_requested'] is True


def test_compatible_extension_seeds_legacy_and_old_v2_writes(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with db.connect(path) as conn:
        db.create_base_schema(conn)
        db.create_trace_schema(conn)
        db.install_schema_guard(conn, 0, None)
        conn.execute('PRAGMA user_version=2')
        insert_job(conn, 'legacy', {'pause_requested': True})
        conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                     ('legacy-track','fixture.mp3','a'*64,'{}',db.utcnow(),db.utcnow()))
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', ('legacy-track','a'*64,'{"sources":[{"provider":"lrclib","state":"candidates"}]}',db.utcnow()))
    db.initialize(path)
    with db.connect(path) as conn:
        db.verify_schema(conn)
        assert json.loads(conn.execute('SELECT public_payload FROM job_status').fetchone()[0])['pause_requested'] is True
        assert json.loads(conn.execute('SELECT sources FROM analysis_status').fetchone()[0]) == [{'provider':'lrclib','state':'candidates'}]
        conn.execute('UPDATE jobs SET payload=? WHERE id=?', ('{"pause_requested":false}', 'legacy'))
    db.initialize(path)
    assert json.loads(db.fetchone(path, 'SELECT public_payload FROM job_status')['public_payload'])['pause_requested'] is False


def test_cleanup_processes_due_changes_once_and_keeps_expiry(client, settings, monkeypatch):
    track_id, revision = seed(settings)
    clock = [time.time()]
    monkeypatch.setattr(policy.time, 'time', lambda: clock[0])
    with db.transaction(settings.db_path) as conn:
        grant(conn, cache_ttl=10)
        candidate = lyrics()
        candidate['usage'] = policy.candidate_usage(conn, candidate)
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track_id, revision, db.encode({'candidates': [candidate]}), db.utcnow()))
    assert policy.cleanup_batch(settings)['scanned'] == 1
    original_transaction = db.transaction
    writes = []
    from contextlib import contextmanager
    @contextmanager
    def observe(path):
        with original_transaction(path) as conn:
            before = conn.total_changes
            yield conn
            writes.append(conn.total_changes - before)
    monkeypatch.setattr(db, 'transaction', observe)
    assert policy.cleanup_batch(settings)['scanned'] == 0
    assert writes == [0]
    clock[0] += 11
    assert policy.cleanup_batch(settings)['changed'] == 1
    data = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM analyses')['payload'])
    assert 'lrc' not in data['candidates'][0] and data['candidates'][0]['transient_only']
    assert policy.cleanup_batch(settings)['scanned'] == 0
    # Same-length edits are invalidated by the database trigger, not timestamps/size.
    with db.transaction(settings.db_path) as conn:
        candidate['lrc'] = '[00:01]Other provider words'
        candidate['usage'] = policy.candidate_usage(conn, candidate)
        conn.execute('UPDATE analyses SET payload=?', (db.encode({'candidates': [candidate]}),))
    assert policy.cleanup_batch(settings)['scanned'] == 1


def test_cleanup_policy_change_and_running_job_transition_survive_restart(client, settings):
    with db.transaction(settings.db_path) as conn:
        grant(conn)
        candidate = lyrics();usage = policy.candidate_usage(conn, candidate)
        proposal = {'lrc': candidate['lrc'], 'field_sources': {'lrc': {'usage': usage}}}
        insert_job(conn, 'running', {'items': [{'proposal': proposal}]}, state='running')
    assert policy.cleanup_batch(settings)['scanned'] == 0
    with db.transaction(settings.db_path) as conn:
        grant(conn, allow_cache=False)
        conn.execute("UPDATE jobs SET state='succeeded' WHERE id='running'")
    db.initialize(settings.db_path)
    assert policy.cleanup_batch(settings)['changed'] == 1
    stored = json.loads(db.fetchone(settings.db_path, "SELECT payload FROM jobs WHERE id='running'")['payload'])
    assert 'lrc' not in stored['items'][0]['proposal']


def test_cleanup_release_reference_uses_its_original_expiry(client, settings, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(policy.time, 'time', lambda: clock[0])
    with db.transaction(settings.db_path) as conn:
        usage = {**policy.rule(conn, 'musicbrainz', 'metadata'), 'origin': 'provider_acquired', 'acquired_at': clock[0], 'expires_at': clock[0] + 5}
        conn.execute('INSERT INTO settings VALUES(?,?)', ('release_review:fixture', db.encode({'reference': {'id': 'fixture'}, 'usage': usage})))
    assert policy.cleanup_batch(settings)['scanned'] == 1
    clock[0] += 6
    assert policy.cleanup_batch(settings)['changed'] == 1
    stored = json.loads(db.setting(settings.db_path, 'release_review:fixture'))
    assert 'reference' not in stored and stored['reference_state'] == 'reference_expired_or_policy_denied'


def test_cleanup_inherited_proposal_deadline_and_policy_shortening(client, settings, monkeypatch):
    track_id, revision = seed(settings)
    clock = [time.time()]
    monkeypatch.setattr(policy.time, 'time', lambda: clock[0])
    with db.transaction(settings.db_path) as conn:
        grant(conn, cache_ttl=100)
        acquired = policy.candidate_usage(conn, lyrics())
        usage = {'origin': 'owner_supplied', 'inherited_restrictions': [acquired]}
        payload = {'lrc': lyrics()['lrc'], 'field_sources': {'lrc': {'usage': usage}}}
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('inherited', track_id, revision, db.encode(payload), db.utcnow()))
    assert policy.cleanup_batch(settings)['scanned'] == 1
    assert policy.cleanup_batch(settings)['scanned'] == 0
    clock[0] += 20
    with db.transaction(settings.db_path) as conn:
        grant(conn, cache_ttl=10)
    assert policy.cleanup_batch(settings)['changed'] == 1
    stored = json.loads(db.fetchone(settings.db_path, "SELECT payload FROM proposals WHERE id='inherited'")['payload'])
    assert 'lrc' not in stored


def test_cleanup_bounds_backlog_and_never_touches_audio(client, settings):
    with db.transaction(settings.db_path) as conn:
        for index in range(80):
            insert_job(conn, f'bounded-{index}', {'items': []})
    first = policy.cleanup_batch(settings, limit=16)
    assert first['scanned'] == 16 and first['pending']
    assert first['music_deleted'] is first['rollback_versions_deleted'] is False
    total = first['scanned']
    while first['pending']:
        first = policy.cleanup_batch(settings, limit=16)
        assert first['scanned'] <= 16
        total += first['scanned']
    assert total == 80
    assert policy.cleanup_batch(settings)['scanned'] == 0


def metric_values(conn, settings):
    return {(s.name, tuple(sorted(s.labels.items()))): s.value
            for family in DatabaseCollector(settings).application(conn, 1800000000)
            for s in family.samples}


def test_metrics_use_compact_status_and_preserve_current_revision_and_cache_lengths(client, settings):
    track_id, revision = seed(settings)
    payload = {'sources': [{'provider': 'lrclib', 'state': 'candidates'}], 'padding': 'x' * 200000}
    with db.transaction(settings.db_path) as conn:
        insert_job(conn, 'metrics', {'padding': 'y' * 200000, 'retry_at': 1800000012},
                   state='queued', stage='waiting_provider')
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track_id, revision, db.encode(payload), db.utcnow()))
        conn.execute('INSERT INTO provider_cache VALUES(?,?,?,?)', ('unicode', 'musicbrainz', 'ю' * 1000, 1900000000))
    with db.connect(settings.db_path) as conn:
        def authorize(action, table, column, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in {'jobs','analyses'} and column == 'payload' else sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        values = metric_values(conn, settings)
    source = ('phonoteka_analysis_sources', (('provider','lrclib'), ('state','candidates')))
    assert values[source] == 1
    assert values[('phonoteka_provider_retry_wait_seconds', (('kind','scan'),))] == 12
    assert values[('phonoteka_provider_cache_bytes', (('provider','musicbrainz'),))] == 1000
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE analyses SET input_sha256=? WHERE track_id=?', ('b'*64, track_id))
        assert source not in metric_values(conn, settings)
        conn.execute('UPDATE analyses SET input_sha256=?,payload=? WHERE track_id=?', (revision, db.encode({'sources': [{'provider': 'lrclib','state': 'no_match'}]}), track_id))
        values = metric_values(conn, settings)
        assert source not in values
        assert values[('phonoteka_analysis_sources', (('provider','lrclib'), ('state','no_match')))] == 1
        conn.execute('DELETE FROM analyses WHERE track_id=?', (track_id,))
        assert not conn.execute('SELECT 1 FROM analysis_status WHERE track_id=?', (track_id,)).fetchone()


def test_analysis_projection_rolls_back_and_cannot_bypass_schema_guard(client, settings):
    track_id, revision = seed(settings)
    with pytest.raises(RuntimeError):
        with db.transaction(settings.db_path) as conn:
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track_id, revision, '{"sources":[]}', db.utcnow()))
            raise RuntimeError('rollback')
    assert not db.fetchone(settings.db_path, 'SELECT 1 FROM analysis_status')
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (track_id, revision, '{"sources":[]}', db.utcnow()))
        conn.create_function('phonoteka_schema_version', 0, lambda: 1)
        with pytest.raises(sqlite3.IntegrityError, match='schema_write_guard'):
            conn.execute('DELETE FROM analysis_status')
