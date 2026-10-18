"""Bounded fault matrix on generated FLAC, never a real collection or provider."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC

from phonoteka import db, maintenance, media
from phonoteka.app import create_app
from phonoteka.worker import Worker
import phonoteka.worker as worker_module
from test_backend import accept_publication_review, authenticated, drain, job, settings, synthetic
from test_media import backup_dir, make_audio, roots, LINUX, TOOLS

pytestmark = [LINUX, TOOLS]


@pytest.mark.parametrize('point', ['backup_copy', 'staged_fsync', 'prepared_journal', 'rename_before', 'rename_after', 'directory_fsync'])
def test_write_fault_preserves_readable_audio_and_recovery_never_replays_without_approval(make_audio, roots, backup_dir, monkeypatch, point):
    path = make_audio('flac')
    source = roots['source'] / path.name; shutil.copy2(path, source)
    original = path.read_bytes(); source_bytes = source.read_bytes(); before = media.sha256_file(path)
    encoded_audio = media.audio_identity(path)
    replaced = False
    original_copy, original_sync, original_journal = media._copy, media._sync_file, media._journal
    original_replace, original_dirsync = media._replace, media._sync_directory
    def copy(source_path, target):
        if point == 'backup_copy' and target.parent == backup_dir:
            raise OSError(errno.ENOSPC, 'injected backup-copy failure')
        return original_copy(source_path, target)
    def sync(target):
        if point == 'staged_fsync' and target.parent.name == '.phonoteka-staging':
            raise OSError(errno.EIO, 'injected staged-file fsync failure')
        return original_sync(target)
    def journal(target, document):
        original_journal(target, document)
        if point == 'prepared_journal' and document['state'] == 'prepared':
            raise OSError(errno.EIO, 'injected interruption after durable intent')
    def rename(source_path, target, **options):
        nonlocal replaced
        if target == path and point == 'rename_before':
            raise OSError(errno.EIO, 'injected rename failure before effect')
        result = original_replace(source_path, target, **options)
        if target == path:
            replaced = True
            if point == 'rename_after':
                raise OSError(errno.EIO, 'injected interruption after rename')
        return result
    def dirsync(directory):
        if point == 'directory_fsync' and directory == path.parent and replaced:
            raise OSError(errno.EIO, 'injected target-directory fsync failure')
        return original_dirsync(directory)
    with monkeypatch.context() as fault:
        fault.setattr(media, '_copy', copy); fault.setattr(media, '_sync_file', sync)
        fault.setattr(media, '_journal', journal); fault.setattr(media, '_replace', rename); fault.setattr(media, '_sync_directory', dirsync)
        with pytest.raises(OSError):
            media.apply_changes(path, {'title': 'Approved new title'}, None, before, backup_dir, roots)
        journals = list(backup_dir.glob('*.json')) if backup_dir.exists() else []
        if point == 'directory_fsync':
            assert len(journals) == 1
            with pytest.raises(OSError): media.recover(journals[0], roots)
            assert json.loads(journals[0].read_text())['state'] != 'committed'
    assert source.read_bytes() == source_bytes
    assert media.audio_identity(path) == encoded_audio
    assert media.audit_audio(path, roots)['state'] == 'passed'
    for backup in backup_dir.glob('*.bak'):
        assert backup.read_bytes() == original
    journals = list(backup_dir.glob('*.json')) if backup_dir.exists() else []
    if journals:
        current = path.read_bytes()
        recovered = media.recover(journals[0], roots)
        assert path.read_bytes() == current  # recovery reconciles, never replays a proposed edit
        if current == original:
            assert recovered['state'] == 'interrupted' and recovered['reason'] == 'new_authorization_required'
        else:
            assert recovered['state'] == 'committed'
        assert Path(recovered['backup']).read_bytes() == original
    else:
        assert path.read_bytes() == original


def test_recovery_restores_missing_target_but_does_not_replace_external_revision(make_audio, roots, backup_dir, monkeypatch):
    path = make_audio('flac'); original = path.read_bytes()
    real_replace = media._replace
    def fail_before_replace(source, target, **options):
        if target == path: raise OSError(errno.EIO, 'simulated lost rename')
        return real_replace(source, target, **options)
    with monkeypatch.context() as fault:
        fault.setattr(media, '_replace', fail_before_replace)
        with pytest.raises(OSError): media.apply_changes(path, {'title': 'Uncommitted'}, None, media.sha256_file(path), backup_dir, roots)
    journal = next(backup_dir.glob('*.json'))
    path.unlink()  # lost directory entry, with a proven readable before-version
    recovered = media.recover(journal, roots)
    assert path.read_bytes() == original and recovered['state'] == 'interrupted'
    assert recovered['reason'] == 'before_restored_new_authorization_required'
    audio = FLAC(path); audio['title'] = ['External owner revision']; audio.save()
    foreign = path.read_bytes()
    assert media.recover(journal, roots)['state'] == 'conflict'
    assert path.read_bytes() == foreign and media.audit_audio(path, roots)['state'] == 'passed'
    assert Path(recovered['backup']).read_bytes() == original


def test_two_real_writes_share_one_atomic_space_budget(make_audio, roots, backup_dir, monkeypatch):
    first = make_audio('flac', 'first.flac'); second = make_audio('flac', 'second.flac')
    before = {path: path.read_bytes() for path in (first, second)}
    for path in before: shutil.copy2(path, roots['source'] / path.name)
    budget = first.stat().st_size * 2 + media._TAG_GROWTH_BUDGET
    monkeypatch.setattr(media.shutil, 'disk_usage', lambda _: SimpleNamespace(free=media._SPACE_MARGIN + budget + 1024))
    waiting, release = threading.Event(), threading.Event()
    def authorize():
        waiting.set()
        assert release.wait(10), 'bounded first write timed out'
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(media.apply_changes, first, {'title': 'First writer'}, None, media.sha256_file(first), backup_dir/'one', roots, authorize=authorize)
        try:
            assert waiting.wait(10), 'first writer never reached its durable intent'
            denied = executor.submit(media.apply_changes, second, {'title': 'Must not fit'}, None, media.sha256_file(second), backup_dir/'two', roots)
            with pytest.raises(media.MediaError) as failure: denied.result(timeout=10)
            assert failure.value.code == 'insufficient_space'
            assert second.read_bytes() == before[second]
        finally:
            release.set()
        result = pending.result(timeout=10)
    assert media.inspect_audio(first, roots)['tags']['title'] == ['First writer']
    assert Path(result['backup_path']).read_bytes() == before[first]
    assert second.read_bytes() == before[second] and not media._SPACE_RESERVED
    assert all((roots['source']/path.name).read_bytes() == contents for path, contents in before.items())


def proposal(client, headers, track, title):
    result = client.post('/api/tracks/' + track['id'] + '/proposal', headers={**headers, 'If-Match': track['sha256']}, json={'tags': {'title': title}})
    assert result.status_code == 201, result.text
    return result.json()


def test_queued_two_applies_and_rollback_have_one_cas_winner_and_intact_history(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    path = synthetic(cfg); source_bytes = (cfg.source_root/path.name).read_bytes()
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); job(client, headers, 'scan'); drain(cfg)
        track = client.get('/api/tracks').json()['items'][0]
        first = proposal(client, headers, track, 'Baseline owner revision')
        job(client, headers, 'apply', [track['id']], {'proposal_id': first['id']}); drain(cfg)
        track = client.get('/api/tracks/' + track['id']).json()
        baseline_history = next(item for item in track['history'] if item['kind'] == 'apply')
        a = proposal(client, headers, track, 'Concurrent intent A'); b = proposal(client, headers, track, 'Concurrent intent B')
        one = job(client, headers, 'apply', [track['id']], {'proposal_id': a['id']})
        two = job(client, headers, 'apply', [track['id']], {'proposal_id': b['id']})
        rollback = job(client, headers, 'rollback', [track['id']], {'history_id': baseline_history['id']})
        drain(cfg)
        assert client.get('/api/jobs/' + one['id']).json()['state'] == 'succeeded'
        assert client.get('/api/jobs/' + two['id']).json()['state'] == 'conflict'
        assert client.get('/api/jobs/' + rollback['id']).json()['state'] == 'conflict'
        current = client.get('/api/tracks/' + track['id']).json()
        assert current['tags']['title'] == 'Concurrent intent A'
        assert len([item for item in current['history'] if item['kind'] == 'apply']) == 2
        assert not [item for item in current['history'] if item['kind'] == 'rollback']
        assert media.audit_audio(path, cfg.roots)['state'] == 'passed'
        assert (cfg.source_root/path.name).read_bytes() == source_bytes


def test_database_commit_failure_reconciles_once_without_rewriting_audio(settings, monkeypatch):
    cfg = replace(settings, read_only=False, storage_verified=True)
    path = synthetic(cfg); source_bytes = (cfg.source_root/path.name).read_bytes()
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); job(client, headers, 'scan'); drain(cfg)
        track = client.get('/api/tracks').json()['items'][0]
        selected = proposal(client, headers, track, 'Committed audio, interrupted database')
        queued = job(client, headers, 'apply', [track['id']], {'proposal_id': selected['id']})
        scope = threading.local(); failed = False; calls = 0
        real_commit, real_persist, real_apply = db.ClosingConnection.commit, Worker.persist_mutation, media.apply_changes
        def commit(connection):
            nonlocal failed
            if getattr(scope, 'persisting', False) and not failed:
                failed = True
                raise sqlite3.OperationalError('injected database commit I/O failure')
            return real_commit(connection)
        def persist(worker, *args, **kwargs):
            scope.persisting = True
            try: return real_persist(worker, *args, **kwargs)
            finally: scope.persisting = False
        def apply(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_apply(*args, **kwargs)
        monkeypatch.setattr(media, 'apply_changes', apply)
        with monkeypatch.context() as fault:
            fault.setattr(db.ClosingConnection, 'commit', commit); fault.setattr(Worker, 'persist_mutation', persist)
            drain(cfg)
        assert failed and calls == 1
        interrupted = client.get('/api/jobs/' + queued['id']).json()
        assert interrupted['state'] == 'interrupted', interrupted
        committed_bytes = path.read_bytes()
        assert not db.fetchall(cfg.db_path, 'SELECT id FROM history WHERE job_id=?', (queued['id'],))
        drain(cfg)
        assert client.get('/api/jobs/' + queued['id']).json()['state'] == 'succeeded'
        assert calls == 1 and path.read_bytes() == committed_bytes
        assert len(db.fetchall(cfg.db_path, 'SELECT id FROM history WHERE job_id=?', (queued['id'],))) == 1
        assert (cfg.source_root/path.name).read_bytes() == source_bytes


def create_three_files(settings):
    first = synthetic(settings)
    for name in ('second.flac', 'third.flac'):
        shutil.copy2(first, settings.work_root/name); shutil.copy2(first, settings.source_root/name)
    return [settings.work_root/name for name in (first.name, 'second.flac', 'third.flac')]


@pytest.mark.parametrize('action', ['pause', 'shutdown'])
def test_real_parallel_writes_finish_or_stop_before_effect_and_ownership_handoff(settings, monkeypatch, action):
    cfg = replace(settings, read_only=False, storage_verified=True, file_workers=2)
    paths = create_three_files(cfg)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); job(client, headers, 'scan'); drain(cfg)
        tracks = client.get('/api/tracks').json()['items']
        selected = tracks[:2] if action == 'shutdown' else tracks
        for track in selected: proposal(client, headers, track, 'Parallel ' + track['relative_path'])
        queued = job(client, headers, 'apply', [track['id'] for track in selected])
        before = {path: path.read_bytes() for path in paths}
        reached, release = threading.Event(), threading.Event(); mutex = threading.Lock(); count = 0; calls = Counter()
        real_apply = media.apply_changes
        def blocked_apply(*args, **kwargs):
            original_authorize = kwargs['authorize']
            with mutex: calls[Path(args[0]).name] += 1
            def authorize():
                nonlocal count
                with mutex:
                    count += 1
                    if count == 2: reached.set()
                assert release.wait(10), 'bounded parallel commit barrier timed out'
                original_authorize()
            return real_apply(*args, **{**kwargs, 'authorize': authorize})
        monkeypatch.setattr(media, 'apply_changes', blocked_apply)
        worker = Worker(cfg)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(lambda: asyncio.run(worker.run(once=True)))
            try:
                assert reached.wait(10), 'two real writes never reached durable intents'
                with pytest.raises(media.MediaError) as contender:
                    with media.writer_guard(cfg.storage_guard_dir, 'competing-instance'): pass
                assert contender.value.code == 'storage_in_use'
                if action == 'pause':
                    assert client.post('/api/jobs/' + queued['id'] + '/pause', headers=headers).status_code == 200
                else:
                    worker.stopping = True
            finally:
                release.set()
            pending.result(timeout=15)
        with media.writer_guard(cfg.storage_guard_dir, 'next-owner'):
            stable = {path: path.read_bytes() for path in paths}
            assert all((cfg.source_root/path.name).read_bytes() == contents for path, contents in before.items())
        if action == 'pause':
            paused = client.get('/api/jobs/' + queued['id']).json()
            assert paused['state'] == 'paused' and paused['completed'] == 2, paused
            assert sum(stable[path] != before[path] for path in paths) == 2
            assert client.post('/api/jobs/' + queued['id'] + '/resume', headers=headers).status_code == 200
            drain(cfg)
            assert client.get('/api/jobs/' + queued['id']).json()['state'] == 'succeeded'
            assert len(calls) == 3 and set(calls.values()) == {1}
        else:
            assert stable == before
            for track in selected:
                journals = list((cfg.state_dir/'backups'/track['id']/queued['id']).glob('*.json'))
                assert len(journals) == 1
                assert media.recover(journals[0], cfg.roots)['state'] == 'interrupted'
            assert not db.fetchall(cfg.db_path, 'SELECT id FROM history WHERE job_id=?', (queued['id'],))


def test_restore_old_state_preserves_newer_work_ready_and_reports_missing_rollback_blob(settings, tmp_path):
    cfg = replace(settings, read_only=False, storage_verified=True)
    path = synthetic(cfg)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); job(client, headers, 'scan'); drain(cfg)
        track = client.get('/api/tracks').json()['items'][0]
        initial = proposal(client, headers, track, 'Checkpoint file revision')
        job(client, headers, 'apply', [track['id']], {'proposal_id': initial['id']}); drain(cfg)
        accept_publication_review(client, headers, track['id']); job(client, headers, 'publish', [track['id']]); drain(cfg)
        checkpoint = tmp_path/'old-state'; maintenance.backup(cfg, checkpoint)
        old_history = db.fetchone(cfg.db_path, "SELECT details FROM history WHERE track_id=? AND kind='apply'", (track['id'],))
        previous_blob = Path(json.loads(old_history['details'])['backup_path']).relative_to(cfg.state_dir)
        track = client.get('/api/tracks/' + track['id']).json()
        later = proposal(client, headers, track, 'Newer actual file revision')
        job(client, headers, 'apply', [track['id']], {'proposal_id': later['id']}); drain(cfg)
        accept_publication_review(client, headers, track['id']); job(client, headers, 'publish', [track['id']]); drain(cfg)
        ready = cfg.publication_root/'Ready'/path.name
        newer_work, newer_ready = path.read_bytes(), ready.read_bytes()
        restored = tmp_path/'restored-old-state'; maintenance.restore(checkpoint, restored)
        (restored/previous_blob).unlink()
        recovered_cfg = replace(cfg, state_dir=restored)
        with TestClient(create_app(recovered_cfg), base_url=cfg.app_url) as recovered:
            authorized = authenticated(recovered, recovered_cfg)
            rejected = recovered.post('/api/jobs', json={'kind': 'publish', 'track_ids': [track['id']]}, headers={**authorized, 'Idempotency-Key': 'no-replay-after-restore'})
            assert rejected.status_code == 409 and rejected.json()['code'] == 'recovery_required'
            history = recovered.get('/api/tracks/' + track['id']).json()['history']
            assert all(not row['rollback_available'] for row in history if row['kind'] == 'apply')
            plan = recovered.get('/api/tracks/' + track['id'] + '/publication?verify=true').json()
            assert plan['destination_integrity'] == 'conflict' and plan['action'] == 'conflict'
            with pytest.raises(media.MediaError) as conflict: maintenance.acknowledge(recovered_cfg)
            assert conflict.value.code == 'conflict'
            assert db.setting(recovered_cfg.db_path, 'recovery_required') == 'true'
        assert path.read_bytes() == newer_work and ready.read_bytes() == newer_ready


def test_scan_keeps_copy_histories_separate_detects_same_size_mtime_and_never_deletes_on_partial(settings, monkeypatch):
    first = synthetic(settings); second = settings.work_root/'copy.flac'; shutil.copy2(first, second)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings); job(client, headers, 'scan'); drain(settings)
        tracks = {track['relative_path']: track for track in client.get('/api/tracks').json()['items']}
        one, two = tracks[first.name], tracks[second.name]
        assert one['sha256'] == two['sha256'] and one['id'] != two['id']
        policy = client.put('/api/tracks/' + one['id'] + '/policy', json={'fields': ['title'], 'base_version': 0}, headers={**headers, 'If-Match': one['sha256']})
        assert policy.status_code == 200
        moved = settings.work_root/'moved.flac'; first.rename(moved)
        job(client, headers, 'scan'); drain(settings)
        current = {track['relative_path']: track for track in client.get('/api/tracks').json()['items']}
        assert current['copy.flac']['id'] == two['id']
        assert current['moved.flac']['id'] not in {one['id'], two['id']}
        assert client.get('/api/tracks/' + current['moved.flac']['id'] + '/policy').json()['locks'] == {}
        assert client.get('/api/tracks/' + one['id'] + '/policy').json()['locks'].get('title')
        stat = moved.stat(); prior = current['moved.flac']
        audio = FLAC(moved); audio['title'] = ['OriginaL']; audio.save()
        assert moved.stat().st_size == stat.st_size
        os.utime(moved, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        job(client, headers, 'scan'); drain(settings)
        changed = next(track for track in client.get('/api/tracks').json()['items'] if track['relative_path'] == 'moved.flac')
        assert changed['sha256'] != prior['sha256'] and changed['id'] != prior['id']
        real_enumerate = worker_module.enumerate_audio
        monkeypatch.setattr(worker_module, 'enumerate_audio', lambda _: (['moved.flac'], [{'code': 'directory_unavailable', 'message': 'injected partial root'}]))
        partial = job(client, headers, 'scan'); drain(settings)
        assert client.get('/api/jobs/' + partial['id']).json()['state'] != 'succeeded'
        assert client.get('/api/tracks/' + two['id']).json()['status'] != 'missing'
        monkeypatch.setattr(worker_module, 'enumerate_audio', real_enumerate)
        settings.work_root.rename(settings.work_root.with_name('temporarily-unavailable'))
        unavailable = job(client, headers, 'scan'); drain(settings)
        assert client.get('/api/jobs/' + unavailable['id']).json()['state'] == 'failed'
        assert client.get('/api/tracks/' + two['id']).json()['status'] != 'missing'
