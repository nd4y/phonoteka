"""Upgrade the deployed v2 contract, rather than only testing empty databases."""
import sqlite3

from fastapi.testclient import TestClient
import pytest

from phonoteka import db
from phonoteka.app import create_app
from test_backend import settings, authenticated

# Deployed ff62cf4d: base SQL plus v2 insert/update/delete write-barrier contract.
DEPLOYED_V2_CHECKSUM = '174b2e2cc0787d3e3e63f006cc56bb2aa966c48446d3b71f361d2080581a0e39'


def deployed_v2(settings):
    assert db.schema_checksum() == DEPLOYED_V2_CHECKSUM
    with db.connect(settings.db_path) as conn:
        db.create_base_schema(conn)
        db.install_schema_guard(conn, 1, 'original-offline-checkpoint')
        conn.execute('PRAGMA user_version=2')
        conn.execute("INSERT INTO settings VALUES('existing-owner-data','preserved')")
    return db.fetchone(settings.db_path, 'SELECT * FROM schema_migrations WHERE version=2')


def test_existing_v2_starts_api_preserves_ledger_and_owner_data(settings):
    ledger = deployed_v2(settings)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        assert client.get('/api/jobs').status_code == 200
    assert db.setting(settings.db_path, 'existing-owner-data') == 'preserved'
    assert db.fetchone(settings.db_path, 'SELECT * FROM schema_migrations WHERE version=2') == ledger
    assert db.fetchall(settings.db_path, 'SELECT * FROM trace_attempts') == []
    db.initialize(settings.db_path)
    assert db.fetchone(settings.db_path, 'SELECT * FROM schema_migrations WHERE version=2') == ledger
    with db.connect(settings.db_path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        db.verify_schema(conn)
        # Previous v2 runtime can still write; older incompatible writers cannot.
        conn.execute("UPDATE settings SET value='v2-compatible' WHERE key='existing-owner-data'")
    with sqlite3.connect(settings.db_path) as old:
        with pytest.raises(sqlite3.DatabaseError, match='phonoteka_schema_version'):
            old.execute("INSERT INTO trace_attempts VALUES('job','item',1,'{}','now')")


def test_extension_and_barriers_roll_back_together(settings, monkeypatch):
    ledger = deployed_v2(settings)
    original = db.install_schema_guard
    def fail(conn, *args):
        original(conn, *args)
        raise OSError('synthetic extension failure before commit')
    monkeypatch.setattr(db, 'install_schema_guard', fail)
    with pytest.raises(OSError, match='synthetic extension failure'):
        db.initialize(settings.db_path)
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='trace_attempts'").fetchone() is None
        db.verify_schema(conn)
    assert db.fetchone(settings.db_path, 'SELECT * FROM schema_migrations WHERE version=2') == ledger
    monkeypatch.setattr(db, 'install_schema_guard', original)
    db.initialize(settings.db_path)
    with db.connect(settings.db_path) as conn:
        db.verify_schema(conn)


@pytest.mark.parametrize('damage', ['ledger', 'barrier'])
def test_invalid_existing_schema_is_rejected_without_repair(settings, damage):
    deployed_v2(settings)
    with db.connect(settings.db_path) as conn:
        if damage == 'ledger':
            conn.execute("UPDATE schema_migrations SET checksum='invalid' WHERE version=2")
        else:
            conn.execute('DROP TRIGGER schema2_settings_insert')
    with pytest.raises(RuntimeError, match='ledger|barrier'):
        db.initialize(settings.db_path)
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='trace_attempts'").fetchone() is None
        if damage == 'ledger':
            assert conn.execute('SELECT checksum FROM schema_migrations WHERE version=2').fetchone()[0] == 'invalid'
        else:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='schema2_settings_insert'").fetchone() is None
