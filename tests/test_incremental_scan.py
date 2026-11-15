"""Real inventory reuse must preserve independent hash/admission evidence."""
from dataclasses import replace
import json
import os
import shutil
import uuid

from fastapi.testclient import TestClient
from mutagen.flac import FLAC
import pytest

from phonoteka import db, media
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import authenticated, drain, job, settings, synthetic


@pytest.fixture
def ready_library(settings):
    cfg = replace(settings, input_grace_seconds=0, file_workers=1)
    path = synthetic(cfg)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        for _ in range(2):
            value = job(client, headers, 'scan'); drain(cfg)
            assert client.get('/api/jobs/' + value['id']).json()['state'] == 'succeeded'
        track = client.get('/api/tracks').json()['items'][0]
        yield cfg, client, headers, path, track


def metadata(cfg, tid):
    return json.loads(db.fetchone(cfg.db_path, 'SELECT metadata FROM tracks WHERE id=?', (tid,))['metadata'])


def test_reuse_does_not_read_audio_write_items_or_advance_hash_and_stability(ready_library, monkeypatch):
    cfg, client, headers, path, track = ready_library
    before = metadata(cfg, track['id'])
    def forbidden(*a, **kw): raise AssertionError('unchanged audio must not be read')
    monkeypatch.setattr(media, 'inspect_audio', forbidden)
    value = job(client, headers, 'scan'); drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert result['state'] == 'succeeded' and result['total'] == result['completed'] == result['failed'] == 0
    assert result['progress'] == 100
    assert result['scan_inventory'] == {'mode':'incremental','discovered':1,'unchanged':1}
    listed = next(x for x in client.get('/api/jobs').json()['items'] if x['id']==value['id'])
    assert listed['scan_inventory'] == result['scan_inventory'] and listed['progress'] == 100
    assert metadata(cfg, track['id']) == before
    assert db.fetchone(cfg.db_path, 'SELECT count(*) AS n FROM job_items WHERE job_id=?', (value['id'],))['n'] == 0


def test_same_size_mtime_edit_is_detected_by_inode_change_time(ready_library):
    cfg, client, headers, path, track = ready_library
    original = path.stat()
    audio = FLAC(path); audio['title'] = ['OriginaL']; audio.save()
    assert path.stat().st_size == original.st_size
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    value = job(client, headers, 'scan'); drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    current = client.get('/api/tracks').json()['items'][0]
    assert result['total'] == 1 and result['scan_inventory']['unchanged'] == 0
    assert current['id'] != track['id'] and current['sha256'] != track['sha256']


def test_new_missing_and_unchanged_paths_keep_distinct_inventory_states(ready_library):
    cfg, client, headers, path, track = ready_library
    other = path.with_name('new.flac'); shutil.copy2(path, other)
    value = job(client, headers, 'scan'); drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert result['total'] == 1 and result['scan_inventory']['unchanged'] == 1
    assert db.fetchone(cfg.db_path, 'SELECT status FROM tracks WHERE id=?', (track['id'],))['status'] != 'missing'
    other.unlink()
    value = job(client, headers, 'scan'); drain(cfg)
    assert client.get('/api/jobs/' + value['id']).json()['scan_inventory']['unchanged'] == 1
    assert db.fetchone(cfg.db_path, "SELECT status FROM tracks WHERE relative_path='new.flac'")['status'] == 'missing'


def test_one_confirmation_still_needs_real_second_full_read(ready_library, monkeypatch):
    cfg, client, headers, path, track = ready_library
    meta = metadata(cfg, track['id']); meta['input_readiness']['confirmations'] = 1
    with db.transaction(cfg.db_path) as c:
        c.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track['id']))
    seen = []; original = media.inspect_audio
    def inspect(*a, **kw): seen.append(str(a[0])); return original(*a, **kw)
    monkeypatch.setattr(media, 'inspect_audio', inspect)
    value = job(client, headers, 'scan'); drain(cfg)
    after = metadata(cfg, track['id'])
    assert len(seen) == 1 and after['input_readiness']['confirmations'] == 2
    assert after['input_readiness']['last_scan_id'] == value['id']


def test_malformed_stat_receipt_requires_real_inventory_instead_of_reuse(ready_library):
    cfg, client, headers, path, track = ready_library
    meta = metadata(cfg, track['id']); meta['inventory_stat'] = {'dev':'invalid'}
    with db.transaction(cfg.db_path) as c:
        c.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track['id']))
    value = job(client, headers, 'scan'); drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert result['state'] == 'succeeded' and result['total'] == result['completed'] == 1
    assert metadata(cfg, track['id'])['inventory_stat']['ino'] == path.stat().st_ino


def test_full_scan_is_explicit_and_invalid_modes_do_not_enqueue(ready_library, monkeypatch):
    cfg, client, headers, path, track = ready_library
    seen = []; original = media.inspect_audio
    def inspect(*a, **kw): seen.append(str(a[0])); return original(*a, **kw)
    monkeypatch.setattr(media, 'inspect_audio', inspect)
    value = job(client, headers, 'scan', params={'scan_mode':'full'}); drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert len(seen) == 1 and result['scan_inventory'] == {'mode':'full','discovered':1,'unchanged':0}
    before = db.fetchone(cfg.db_path, 'SELECT count(*) AS n FROM jobs')['n']
    for kind, params in [('scan',{'scan_mode':'unknown'}),('audit',{'scan_mode':'full'}),('scan',{'scan_mode':True}),('scan',{'scan_mode':'full','reconcile_root':True})]:
        response = client.post('/api/jobs', json={'kind':kind,'params':params}, headers={**headers,'Idempotency-Key':str(uuid.uuid4())})
        assert response.status_code == 422, response.text
    assert db.fetchone(cfg.db_path, 'SELECT count(*) AS n FROM jobs')['n'] == before


def test_accepted_legacy_scan_without_mode_keeps_full_behavior(ready_library, monkeypatch):
    cfg, client, headers, path, track = ready_library
    value = job(client, headers, 'scan')
    with db.transaction(cfg.db_path) as c:
        payload = json.loads(c.execute('SELECT payload FROM jobs WHERE id=?',(value['id'],)).fetchone()[0])
        del payload['params']['scan_mode']
        c.execute('UPDATE jobs SET payload=? WHERE id=?',(db.encode(payload),value['id']))
    seen = []; original = media.inspect_audio
    def inspect(*a, **kw): seen.append(str(a[0])); return original(*a, **kw)
    monkeypatch.setattr(media, 'inspect_audio', inspect)
    drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert len(seen) == 1 and result['scan_inventory']['mode'] == 'full'


def test_bad_inputs_are_retried_without_resetting_unchanged_good_proofs(ready_library):
    cfg, client, headers, path, track = ready_library
    before = metadata(cfg, track['id']); path.with_name('broken.flac').write_bytes(b'x')
    for _ in range(2):
        value = job(client, headers, 'scan'); drain(cfg)
        result = client.get('/api/jobs/' + value['id']).json()
        assert result['state'] == 'completed_with_errors' and result['failed'] == result['total'] == 1
        assert result['scan_inventory']['unchanged'] == 1
        assert metadata(cfg, track['id']) == before


def test_edit_during_other_file_inspection_invalidates_planned_skip(ready_library, monkeypatch):
    cfg, client, headers, path, track = ready_library
    other = path.with_name('new.flac'); shutil.copy2(path, other)
    original = Worker.scan_one; seen = []
    def inspect(worker, relative, scan_id, expected=None):
        result = original(worker, relative, scan_id, expected)
        seen.append(relative)
        if relative == other.name:
            audio = FLAC(path); audio['title'] = ['Changed while scanning']; audio.save()
        return result
    monkeypatch.setattr(Worker, 'scan_one', inspect)
    value = job(client, headers, 'scan'); drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert seen == [other.name, path.name]
    assert result['total'] == result['completed'] == 2 and result['scan_inventory']['unchanged'] == 0


def test_cancelled_pass_does_not_mint_admission_or_invalidate_prior_stable_skips(ready_library, monkeypatch):
    cfg, client, headers, path, track = ready_library
    before = metadata(cfg, track['id']); other = path.with_name('new.flac'); shutil.copy2(path, other)
    original = Worker.scan_one
    def inspect(worker, relative, scan_id, expected=None):
        result = original(worker, relative, scan_id, expected)
        with db.transaction(cfg.db_path) as c: c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (scan_id,))
        return result
    monkeypatch.setattr(Worker, 'scan_one', inspect)
    value = job(client, headers, 'scan'); drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert result['state'] == 'cancelled_partial'
    assert metadata(cfg, track['id']) == before
    new = db.fetchone(cfg.db_path, "SELECT id FROM tracks WHERE relative_path='new.flac'")
    assert metadata(cfg, new['id'])['input_readiness']['confirmations'] == 0


@pytest.mark.parametrize('changed', [False, True])
def test_durable_scan_receipt_is_joined_or_rechecked_after_change(ready_library, monkeypatch, changed):
    cfg, client, headers, path, track = ready_library
    meta = metadata(cfg, track['id']); meta['input_readiness']['confirmations'] = 1
    with db.transaction(cfg.db_path) as c: c.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track['id']))
    value = job(client, headers, 'scan')
    worker = Worker(cfg)
    try:
        result = worker.scan_one(path.name, value['id'])
        worker.item_result(value['id'], path.name, 'succeeded', result)
    finally: worker.pool.shutdown(wait=True)
    if changed:
        audio = FLAC(path); audio['title'] = ['External edit during interruption']; audio.save()
    original = media.inspect_audio; seen = []
    def inspect(*a, **kw): seen.append(str(a[0])); return original(*a, **kw)
    monkeypatch.setattr(media, 'inspect_audio', inspect)
    drain(cfg)
    result = client.get('/api/jobs/' + value['id']).json()
    assert result['state'] == 'succeeded' and result['completed'] == result['total'] == 1
    assert len(seen) == int(changed)
    current = client.get('/api/tracks').json()['items'][0]
    assert (current['sha256'] != track['sha256']) == changed


def test_cue_inventory_is_revalidated_for_reference_dependencies(ready_library):
    cfg, client, headers, path, track = ready_library
    sheet = path.with_suffix('.cue')
    sheet.write_text('FILE "sample.flac" WAVE\nTRACK 01 AUDIO\nTITLE "Song"\nINDEX 01 00:00:00\n', encoding='utf-8')
    for _ in range(2):
        job(client, headers, 'scan'); drain(cfg)
    path.unlink()
    value = job(client, headers, 'scan'); drain(cfg)
    row = db.fetchone(cfg.db_path, 'SELECT id FROM tracks WHERE relative_path=? AND is_current=1', (sheet.name,))
    assert metadata(cfg, row['id'])['cue']['errors']
    assert db.fetchone(cfg.db_path, 'SELECT status FROM tracks WHERE id=?', (track['id'],))['status'] == 'missing'
