import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import pytest

from phonoteka import db,media,migrations,retention
from phonoteka.worker import scheduler_guard
from test_backend import settings


pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Reviewed migration/storage locks require Linux')


def legacy(settings):
    """A real v1 database: remove only the new ledger/barriers, retain old data."""
    db.initialize(settings.db_path)
    with db.connect(settings.db_path) as conn:
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'schema2_%'").fetchall():
            conn.execute('DROP TRIGGER "'+row[0]+'"')
        conn.execute('DROP TABLE schema_migrations')
        conn.execute('PRAGMA user_version=1')
        conn.execute("INSERT INTO settings VALUES('legacy-owner-state','retained')")
        conn.commit()


def version(path):
    with sqlite3.connect(path) as conn:return conn.execute('PRAGMA user_version').fetchone()[0]


def test_empty_database_has_versioned_barrier_and_legacy_connection_is_read_only(settings):
    db.initialize(settings.db_path)
    assert version(settings.db_path)==2
    with db.connect(settings.db_path) as conn:
        db.verify_schema(conn)
        conn.execute("INSERT INTO settings VALUES('supported-writer','ok')")
    with sqlite3.connect(settings.db_path) as old:
        assert old.execute("SELECT value FROM settings WHERE key='supported-writer'").fetchone()[0]=='ok'
        with pytest.raises(sqlite3.DatabaseError,match='phonoteka_schema_version'):
            old.execute("INSERT INTO settings VALUES('legacy-writer','forbidden')")
    assert db.setting(settings.db_path,'legacy-writer') is None


def test_v1_startup_requires_explicit_offline_transition_and_held_old_connection_cannot_write(settings):
    legacy(settings)
    with pytest.raises(RuntimeError,match='phonoteka.migrations migrate'):
        db.initialize(settings.db_path)
    assert version(settings.db_path)==1
    with sqlite3.connect(settings.db_path) as old:
        old.execute('SELECT * FROM settings').fetchall()
        result=migrations.migrate(settings,confirm_stopped=True)
        assert result['schema']==2 and result['changed'] and not result['music_changed']
        with pytest.raises(sqlite3.DatabaseError,match='phonoteka_schema_version'):
            old.execute("INSERT INTO settings VALUES('stale-executor','forbidden')")
    assert db.setting(settings.db_path,'legacy-owner-state')=='retained'
    entry=db.fetchone(settings.db_path,'SELECT * FROM schema_migrations WHERE version=2')
    assert entry['source_version']==1 and entry['checkpoint_id']==result['checkpoint_id']


@pytest.mark.parametrize('phase',['after_checkpoint','after_ddl','before_version','before_commit'])
def test_failed_transition_rolls_back_schema_keeps_verified_checkpoint_and_blocks_startup(settings,phase):
    legacy(settings)
    def fail(current):
        if current==phase:raise OSError('synthetic migration fault')
    with pytest.raises(OSError):migrations.migrate(settings,confirm_stopped=True,fault=fail)
    assert version(settings.db_path)==1
    marker=json.loads(migrations.marker_path(settings).read_text())
    assert marker['state']=='failed'
    assert migrations.verify_checkpoint(marker['checkpoint_path'])['source_schema']==1
    with pytest.raises(RuntimeError):db.connect(settings.db_path)
    with pytest.raises(ValueError,match='--retry'):migrations.migrate(settings,confirm_stopped=True)
    assert migrations.migrate(settings,confirm_stopped=True,retry=True)['schema']==2


def test_commit_uncertainty_is_blocked_until_explicit_schema_and_checkpoint_reconciliation(settings):
    legacy(settings)
    def fail(current):
        if current=='after_commit':raise OSError('synthetic post-commit sync failure')
    with pytest.raises(OSError):migrations.migrate(settings,confirm_stopped=True,fault=fail)
    assert version(settings.db_path)==2
    with pytest.raises(RuntimeError,match='Migration recovery required'):db.connect(settings.db_path)
    assert migrations.migrate(settings,confirm_stopped=True,retry=True)=={'schema':2,'changed':False,'verified':True}
    assert db.setting(settings.db_path,'legacy-owner-state')=='retained'


def test_checkpoint_excludes_raw_cache_and_credentials_and_pins_blob_dependencies(settings,tmp_path):
    legacy(settings)
    identity=str(uuid.uuid4());directory=settings.state_dir/'exports'/identity;directory.mkdir(parents=True)
    (directory/'manifest.json').write_text(json.dumps({'id':identity,'created_at':'2000-01-01T00:00:00+00:00','snapshot_available':True,'files':[]}))
    old=time.time()-86400*60;os.utime(directory/'manifest.json',(old,old))
    with db.connect(settings.db_path,maintenance=True) as conn:
        conn.execute("INSERT INTO provider_cache VALUES('private','lrclib','private-checkpoint-cache-value',9999999999)")
        conn.commit()
    result=migrations.migrate(settings,confirm_stopped=True)
    checkpoint=settings.state_dir/'migrations'/result['checkpoint_id']
    manifest=migrations.verify_checkpoint(checkpoint)
    assert not manifest['blobs_copied'] and not manifest['music_included']
    assert b'private-checkpoint-cache-value' not in (checkpoint/'checkpoint.sqlite3').read_bytes()
    plan=retention.preview(settings,verify_files=True)
    artifact=next(item for item in plan['objects'] if item['id']=='export:'+identity)
    assert not artifact['eligible'] and 'maintenance_checkpoint_reference' in artifact['protected_reasons']
    destination=tmp_path/'restored-checkpoint'
    restored=migrations.restore_checkpoint(settings,checkpoint,destination,confirm_stopped=True)
    assert restored['schema']==1 and restored['recovery_required'] and restored['credentials_revoked']
    with db.connect(destination/'phonoteka.sqlite3',maintenance=True) as conn:
        assert conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()[0]=='true'
        assert not conn.execute('SELECT 1 FROM sessions').fetchone() and not conn.execute('SELECT 1 FROM api_tokens').fetchone()
    assert (directory/'manifest.json').exists()
    (directory/'manifest.json').write_text('changed dependency')
    with pytest.raises(ValueError,match='dependency'):migrations.verify_checkpoint(checkpoint)
    with pytest.raises(ValueError):migrations.restore_checkpoint(settings,checkpoint,tmp_path/'invalid-restore',confirm_stopped=True)
    assert not (tmp_path/'invalid-restore').exists()


def test_migration_requires_stopped_scheduler_and_rejects_concurrent_api_state_change(settings):
    legacy(settings)
    with pytest.raises(ValueError,match='--confirm-stopped'):migrations.migrate(settings)
    with scheduler_guard(settings.state_dir),pytest.raises(RuntimeError):
        migrations.migrate(settings,confirm_stopped=True)
    def external_write(phase):
        if phase=='after_checkpoint':
            with sqlite3.connect(settings.db_path) as other:other.execute("INSERT INTO settings VALUES('concurrent-api','changed')")
    with pytest.raises(ValueError,match='State changed after checkpoint'):
        migrations.migrate(settings,confirm_stopped=True,fault=external_write)
    assert version(settings.db_path)==1
