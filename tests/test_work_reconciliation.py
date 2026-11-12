"""Full-byte Work rebinding is explicit, conservative and not a scan pass."""
import asyncio
from dataclasses import replace
import hashlib
import json
import uuid

import pytest

from phonoteka import db, media, work_reconciliation as reconciliation
from phonoteka.worker import Worker, root_identity
from test_backend import settings, drain, job as enqueue
from test_confirmed_fields import library


def add(cfg, name, content=b'raw current bytes', status='indexed', *, present=True):
    path = cfg.work_root / name
    if present:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    identity = str(uuid.uuid4()); sha = hashlib.sha256(content).hexdigest()
    metadata = {'sha256': sha, 'tags': {'title': ['Preserve']}, 'input_readiness': {'sentinel': 'unchanged'},
                'recording_classification': {'origin': 'owner', 'kind': 'single_recording'}}
    with db.transaction(cfg.db_path) as conn:
        conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                     (identity, name, sha, db.encode(metadata), status, db.utcnow(), db.utcnow()))
    return identity, path


def start(cfg):
    current = root_identity(cfg.work_root)
    old = {**current, 'device': current['device'] + 1}
    identity = str(uuid.uuid4()); stamp = db.utcnow()
    with db.transaction(cfg.db_path) as conn:
        epoch = int(conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone()[0])
        conn.execute("INSERT OR REPLACE INTO settings VALUES('work_root_identity',?)", (db.encode(old),))
        conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,created_at,updated_at,auth_epoch) VALUES(?,'scan','running','scan',?,?,?,?)",
                     (identity, db.encode({'items': [], 'params': {reconciliation.PARAM: True}}), stamp, stamp, epoch))
        job = dict(conn.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone())
    return job, old, current


def run(cfg, job):
    worker = Worker(cfg)
    try:
        return asyncio.run(reconciliation.run(worker, job))
    finally:
        worker.pool.shutdown(wait=True)


def rows(cfg):
    with db.connect(cfg.db_path) as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM tracks ORDER BY id')]


@pytest.fixture
def raw(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    db.initialize(cfg.db_path, settings=cfg)
    add(cfg, 'a.flac')
    return cfg


def assert_not_rebound(cfg, old):
    assert json.loads(db.setting(cfg.db_path, 'work_root_identity')) == old
    assert db.setting(cfg.db_path, reconciliation.PROOF) is None


def test_full_current_inventory_success_preserves_all_rows_and_bytes(raw):
    cfg = raw
    add(cfg, 'broken.cue', b'FILE broken', 'error')
    absent, _ = add(cfg, 'known-missing.mp3', status='missing', present=False)
    add(cfg, 'returned.mp3', b'returned bytes', status='missing')
    before = rows(cfg); original = {r['relative_path']: (cfg.work_root/r['relative_path']).read_bytes() for r in before if (cfg.work_root/r['relative_path']).is_file()}
    job, old, current = start(cfg)
    proof = run(cfg, job)
    assert proof['count'] == 4 and proof['anchors'] == 3 and proof['known_missing_absent'] == 1
    assert proof['manifest_sha256'] and proof['from'] == old and proof['to'] == current
    assert json.loads(db.setting(cfg.db_path, 'work_root_identity')) == current
    assert json.loads(db.setting(cfg.db_path, reconciliation.PROOF)) == proof
    assert rows(cfg) == before
    assert all((cfg.work_root/p).read_bytes() == data for p, data in original.items())
    with db.connect(cfg.db_path) as conn:
        done = dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
        assert done['state'] == 'succeeded' and done['completed'] == 4 and done['failed'] == 0
        missing = json.loads(conn.execute('SELECT result FROM job_items WHERE job_id=? AND item_key=?', (job['id'], absent)).fetchone()[0])
        assert missing['skipped'] is True and missing['hashed'] is False


def test_native_library_keeps_real_two_scan_readiness_and_existing_drafts(library):
    cfg, client, headers, tid, sha, path = library
    before = rows(cfg); job, old, current = start(cfg)
    run(cfg, job)
    assert rows(cfg) == before and media.sha256_file(path) == sha


@pytest.mark.parametrize('change', ['bytes', 'missing', 'symlink', 'hardlink', 'alias'])
def test_invalid_or_changed_file_does_not_rebind(raw, change):
    cfg = raw; first = cfg.work_root/'a.flac'
    if change == 'bytes': first.write_bytes(b'different same path')
    if change == 'missing': first.unlink()
    if change == 'symlink':
        target = cfg.work_root/'target'; target.write_bytes(first.read_bytes()); first.unlink()
        try: first.symlink_to(target)
        except OSError: pytest.skip('Symlink unavailable on this platform')
    if change == 'hardlink': (cfg.work_root/'hardlinked').hardlink_to(first)
    if change == 'alias':
        with db.transaction(cfg.db_path) as conn:
            conn.execute("UPDATE tracks SET relative_path='sub/../a.flac'")
    job, old, current = start(cfg)
    with pytest.raises((media.MediaError, OSError)): run(cfg, job)
    assert_not_rebound(cfg, old)


@pytest.mark.parametrize('change', ['inode', 'path', 'fresh_receipts', 'empty', 'all_missing'])
def test_invalid_anchor_scope_fails_closed(raw, change):
    cfg = raw; job, old, current = start(cfg)
    with db.transaction(cfg.db_path) as conn:
        if change in {'inode', 'path'}:
            old = {**old, **({'inode': old['inode'] + 1} if change == 'inode' else {'path': old['path'] + '-different'})}
            conn.execute("UPDATE settings SET value=? WHERE key='work_root_identity'", (db.encode(old),))
        if change == 'fresh_receipts':
            conn.execute('INSERT INTO job_items VALUES(?,?,?,?)', (job['id'], 'prior', 'succeeded', '{}'))
        if change == 'empty': conn.execute('DELETE FROM tracks')
        if change == 'all_missing': conn.execute("UPDATE tracks SET status='missing'")
    if change == 'all_missing': (cfg.work_root/'a.flac').unlink()
    with pytest.raises(media.MediaError): run(cfg, job)
    assert_not_rebound(cfg, old)


@pytest.mark.parametrize('change', ['cancel', 'epoch', 'root', 'catalog', 'binding', 'recovery', 'late_bytes', 'known_absence'])
def test_mid_verification_changes_leave_old_root_untouched(raw, monkeypatch, change):
    cfg = raw
    if change == 'known_absence': add(cfg, 'known.mp3', b'previous missing bytes', 'missing', present=False)
    job, old, current = start(cfg); original = reconciliation._verify_one
    changed = False
    def verify(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            changed = True
            with db.transaction(cfg.db_path) as conn:
                if change == 'cancel': conn.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job['id'],))
                if change == 'epoch': conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
                if change == 'catalog': conn.execute("UPDATE tracks SET status='needs_review'")
                if change == 'binding': conn.execute("UPDATE settings SET value=? WHERE key='work_root_identity'", (db.encode({**old, 'device': old['device'] + 10}),))
                if change == 'recovery': conn.execute("INSERT OR REPLACE INTO settings VALUES('recovery_required','true')")
            if change == 'root': monkeypatch.setattr('phonoteka.worker.root_identity', lambda _: {**current, 'device': current['device'] + 10})
            if change == 'late_bytes': (cfg.work_root/'a.flac').write_bytes(b'changed after full hash')
            if change == 'known_absence' and not result['exists']: (cfg.work_root/'known.mp3').write_bytes(b'reappeared')
        if change == 'known_absence' and not result['exists']: (cfg.work_root/'known.mp3').write_bytes(b'reappeared')
        return result
    monkeypatch.setattr(reconciliation, '_verify_one', verify)
    with pytest.raises(media.MediaError): run(cfg, job)
    if change == 'binding':
        assert json.loads(db.setting(cfg.db_path, 'work_root_identity')) == {**old, 'device': old['device'] + 10}
        assert db.setting(cfg.db_path, reconciliation.PROOF) is None
    else: assert_not_rebound(cfg, old)


def test_root_already_current_is_noop_without_fabricated_hash_results(raw, monkeypatch):
    cfg = raw; job, old, current = start(cfg)
    db.set_setting(cfg.db_path, 'work_root_identity', db.encode(current))
    def unexpected(*_): raise AssertionError('No bytes should be claimed as hashed')
    monkeypatch.setattr(reconciliation, '_verify_one', unexpected)
    assert run(cfg, job) == {'state': 'already_current', 'hashes': 0}
    with db.connect(cfg.db_path) as conn:
        assert not conn.execute('SELECT 1 FROM job_items WHERE job_id=?', (job['id'],)).fetchone()
        assert conn.execute('SELECT state,total FROM jobs WHERE id=?', (job['id'],)).fetchone()['total'] == 0
    assert db.setting(cfg.db_path, reconciliation.PROOF) is None


@pytest.mark.parametrize('payload', [
    {'items': [], 'params': {}},
    {'items': [], 'params': {reconciliation.PARAM: True, 'folder': 'a'}},
    {'items': [], 'params': {reconciliation.PARAM: True}, 'auth_token_id': 'token'},
    {'items': [], 'params': {reconciliation.PARAM: True}, 'auth_limited': True},
])
def test_non_owner_or_non_explicit_job_is_rejected(raw, payload):
    cfg = raw; job, old, current = start(cfg)
    job['payload'] = db.encode(payload)
    with db.transaction(cfg.db_path) as conn:
        conn.execute('UPDATE jobs SET payload=? WHERE id=?', (job['payload'], job['id']))
    with pytest.raises(media.MediaError): run(cfg, job)
    assert_not_rebound(cfg, old)


def test_owner_api_root_flag_and_explicit_job(library):
    cfg, client, headers, tid, sha, path = library
    current = root_identity(cfg.work_root); old = {**current, 'device': current['device'] + 1}
    db.set_setting(cfg.db_path, 'work_root_identity', db.encode(old))
    assert client.get('/api/metadata-preparation').json()['root_reconciliation_required'] is True
    result = enqueue(client, headers, 'scan', params={reconciliation.PARAM: True})
    drain(cfg)
    assert client.get('/api/jobs/' + result['id']).json()['state'] == 'succeeded'
    assert client.get('/api/metadata-preparation').json()['root_reconciliation_required'] is False
    assert media.sha256_file(path) == sha


@pytest.mark.parametrize('kind,ids,params', [
    ('audit', [], {reconciliation.PARAM: True}),
    ('scan', ['11111111-1111-4111-8111-111111111111'], {reconciliation.PARAM: True}),
    ('scan', [], {reconciliation.PARAM: 1}),
    ('scan', [], {reconciliation.PARAM: True, 'folder': ''}),
])
def test_api_rejects_inexact_reconciliation_request(library, kind, ids, params):
    cfg, client, headers, tid, sha, path = library
    response = client.post('/api/jobs', json={'kind': kind, 'track_ids': ids, 'params': params},
                          headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
    assert response.status_code == 422, response.text


def test_ordinary_scan_keeps_existing_mismatch_guard(library):
    cfg, client, headers, tid, sha, path = library
    current = root_identity(cfg.work_root); old = {**current, 'device': current['device'] + 1}
    before = rows(cfg)
    db.set_setting(cfg.db_path, 'work_root_identity', db.encode(old))
    result = enqueue(client, headers, 'scan')
    drain(cfg)
    native = client.get('/api/jobs/' + result['id']).json()
    assert native['state'] == 'failed' and native['error'] == 'root_identity_changed'
    assert_not_rebound(cfg, old)
    assert rows(cfg) == before
