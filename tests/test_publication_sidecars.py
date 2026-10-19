"""Real temporary filesystem sidecars. Generated 0.2 s FLAC, no music/network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
from mutagen.flac import FLAC, Picture
from PIL import Image
import pytest

from phonoteka import comparison, db, exports, lyrics_review, media, provider_policy, publication, publication_paths, publication_sidecars as sidecars
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, accept_publication_review, job, drain

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'), reason='Linux durable mutation profile and ffmpeg')


def generated(settings, client, headers, relative='Album/song.flac', *, color='red', provenance=None):
    path = settings.work_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=8000:duration=0.2', '-c:a', 'flac', str(path)], check=True)
    audio = FLAC(path)
    audio['title'] = ['Generated fixture']
    audio['artist'] = ['Test owner']
    audio['album'] = ['Fixture album']
    audio['LYRICS'] = ['[00:00.010]Generated line']
    raw = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(raw, 'JPEG')
    picture = Picture(); picture.type = 3; picture.mime = 'image/jpeg'; picture.width = picture.height = 16; picture.depth = 24; picture.data = raw.getvalue()
    audio.add_picture(picture); audio.save()
    meta = media.inspect_audio(path, settings.roots)
    sha = meta['sha256']; identity = str(uuid.uuid4())
    # Audit quality is tested separately; this fixture tests publication effects.
    meta['audit'] = {'state': 'passed', 'sha256': sha, 'fixture': 'synthetic_sidecar_transaction'}
    meta['usage_provenance'] = provenance or {}
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)', (identity, relative, sha, db.encode(meta), db.utcnow(), db.utcnow()))
    accept_publication_review(client, headers, identity, profile='lyrics_required', reason='')
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (identity,))
    gate = lyrics_review.assert_publish_allowed(settings, row)
    meta = json.loads(row['metadata'])
    meta['publication'] = {'relative_path': relative, 'sha256': sha, 'profile': gate['profile'],
                           'review_version': gate['review_version'], 'document_revision': gate['document_revision']}
    target = settings.publication_root / 'Ready' / relative
    target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, target)
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), identity))
    return identity, sha


def owner(settings):
    return replace(settings, read_only=False, storage_verified=True)


def test_opt_in_plan_owner_auth_and_worker_publishes_all_objects(settings):
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        assert client.get('/api/publication/sidecars').status_code == 401
        headers = authenticated(client, cfg)
        tid, sha = generated(cfg, client, headers)
        assert client.get('/api/publication/sidecars').json() == {'default_enabled': False, 'items': []}
        body = {'track_ids': [tid]}
        assert client.post('/api/publication/sidecars/plans', json=body, headers=headers).status_code == 409
        body.update(cover=True, lrc=True)
        response = client.post('/api/publication/sidecars/plans', json=body, headers=headers)
        assert response.status_code == 201, response.text
        plan = response.json(); assert plan['multi_file_atomic'] is False
        assert not (cfg.publication_root / 'Ready/Album/cover.jpg').exists()
        queued = job(client, headers, 'publish_sidecars', [tid], {'plan_id': plan['id']})
        drain(cfg)
        status = client.get('/api/jobs/' + queued['id']).json()
        assert status['state'] == 'succeeded' and status['completed'] == 3, status
        completed = client.get('/api/publication/sidecars/plans/' + plan['id']).json()
        assert completed['state'] == 'complete' and len(completed['results']) == 2
        assert (cfg.publication_root / 'Ready/Album/song.lrc').read_text() == '[00:00.010]Generated line\n'
        assert media.sha256_file(cfg.work_root / 'Album/song.flac') == sha
        assert media.sha256_file(cfg.publication_root / 'Ready/Album/song.flac') == sha
        assert not db.fetchall(cfg.db_path, "SELECT id FROM history WHERE kind='apply'")


def test_classification_change_invalidates_already_queued_sidecar_plan(settings):
    cfg=owner(settings)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);tid,sha=generated(cfg,client,headers)
        plan=sidecars.create_plan(cfg,[tid],cover=True,lrc=True)
        queued=job(client,headers,'publish_sidecars',[tid],{'plan_id':plan['id']})
        response=client.put(f'/api/tracks/{tid}/recording-classification',
            json={'kind':'possible_mix','base_version':0,'reason':'Owner identified several different songs in this file'},
            headers={**headers,'If-Match':f'"{sha}"'})
        assert response.status_code==200,response.text
        row=db.fetchone(cfg.db_path,'SELECT * FROM tracks WHERE id=?',(tid,))
        gate=lyrics_review.publication_gate(cfg,row)
        assert gate['state']=='blocked' and 'possible_mix_requires_manual_review' in gate['reasons']
        drain(cfg)
        assert client.get('/api/jobs/'+queued['id']).json()['state']!='succeeded'
        assert not (cfg.publication_root/'Ready/Album/cover.jpg').exists()
        assert not (cfg.publication_root/'Ready/Album/song.lrc').exists()
        assert not sidecars.load_plan(cfg,plan['id'])['results']
        assert media.sha256_file(cfg.work_root/'Album/song.flac')==sha
        assert media.sha256_file(cfg.publication_root/'Ready/Album/song.flac')==sha


def test_concurrent_conflicting_album_cover_plans_do_not_overwrite(settings):
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        first, _ = generated(cfg, client, headers, 'Album/one.flac', color='red')
        second, _ = generated(cfg, client, headers, 'Album/two.flac', color='blue')
        with pytest.raises(media.MediaError, match='несовместимый общий sidecar'):
            sidecars.create_plan(cfg, [first, second], cover=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(sidecars.create_plan, cfg, [first], cover=True, lrc=True)
            b = pool.submit(sidecars.create_plan, cfg, [second], cover=True, lrc=True)
            plans = [a.result(), b.result()]
        # The worker's ordinary scheduler lane serializes these commits. Both
        # plans were frozen before either effect, so the second must fail CAS.
        sidecars.commit(cfg, plans[0]['id'])
        original = (cfg.publication_root / 'Ready/Album/cover.jpg').read_bytes()
        with pytest.raises(media.MediaError):
            sidecars.commit(cfg, plans[1]['id'])
        assert (cfg.publication_root / 'Ready/Album/cover.jpg').read_bytes() == original
        assert not (cfg.publication_root / 'Ready/Album/two.lrc').exists()
        with db.connect(cfg.db_path) as conn:
            registry = sidecars.saved(conn, sidecars.registry_key('Album/cover.jpg'))
        assert set(registry['owners']) == {first}


@pytest.mark.parametrize(('first', 'second'), [('Album/Song.flac', 'album/song.flac'), ('Album/é.flac', 'Album/e\u0301.flac'), ('Album/one.flac', 'album/two.flac')])
def test_real_ready_unicode_case_collisions_prevent_sidecar_effect(settings, first, second):
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        a, _ = generated(cfg, client, headers, first)
        b, _ = generated(cfg, client, headers, second)
        with pytest.raises(media.MediaError) as failure:
            sidecars.create_plan(cfg, [a, b], cover=True)
        assert failure.value.code in {'publication_path_conflict', 'sidecar_shared_path_conflict'}
        assert not list((cfg.publication_root / 'Ready').rglob('cover.jpg'))


def test_lrc_same_stem_conflict_and_cross_platform_names():
    for name in ['Album/CON.mp3', 'Album/a?.mp3', 'Album/a.mp3 ', 'Album/a\nb.mp3', 'Album/../a.mp3']:
        with pytest.raises(media.MediaError): publication_paths.key(name)
    paths = {}; publication_paths.add(paths, 'Album/song.lrc')
    with pytest.raises(media.MediaError): publication_paths.add(paths, 'Album/song.lrc/sub/file')


def test_failure_after_audio_and_first_sidecar_is_visible_blocks_export_and_resumes(settings, monkeypatch):
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, sha = generated(cfg, client, headers)
        plan = sidecars.create_plan(cfg, [tid], cover=True, lrc=True)
        original = media.publish_bytes
        def failing(data, destination, *args, **kwargs):
            if str(destination).endswith('.lrc'): raise OSError('injected sidecar failure after committed cover and audio')
            return original(data, destination, *args, **kwargs)
        monkeypatch.setattr(media, 'publish_bytes', failing)
        queued = job(client, headers, 'publish_sidecars', [tid], {'plan_id': plan['id']}); drain(cfg)
        assert client.get('/api/jobs/' + queued['id']).json()['state'] == 'completed_with_errors'
        assert sidecars.load_plan(cfg, plan['id'])['state'] == 'partial'
        assert media.sha256_file(cfg.publication_root / 'Ready/Album/song.flac') == sha
        with pytest.raises(media.MediaError) as blocked: exports.generate(cfg)
        assert blocked.value.code == 'sidecar_partial_publication'
        monkeypatch.setattr(media, 'publish_bytes', original)
        retried = job(client, headers, 'publish_sidecars', [tid], {'plan_id': plan['id']}); drain(cfg)
        assert client.get('/api/jobs/' + retried['id']).json()['state'] == 'succeeded'
        assert sidecars.load_plan(cfg, plan['id'])['state'] == 'complete'


def test_restart_after_file_commit_before_registry_requires_fresh_owner_retry(settings, monkeypatch):
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, _ = generated(cfg, client, headers)
        plan = sidecars.create_plan(cfg, [tid], cover=True)
        original = sidecars.record
        monkeypatch.setattr(sidecars, 'record', lambda *args: (_ for _ in ()).throw(OSError('DB failure after durable journal')))
        queued = job(client, headers, 'publish_sidecars', [tid], {'plan_id': plan['id']}); drain(cfg)
        target = cfg.publication_root / 'Ready/Album/cover.jpg'; inode = target.stat().st_ino
        assert sidecars.load_plan(cfg, plan['id'])['state'] == 'interrupted'
        with pytest.raises(media.MediaError): exports.generate(cfg)
        with db.transaction(cfg.db_path) as conn: conn.execute("UPDATE jobs SET state='running' WHERE id=?", (queued['id'],))
        worker = Worker(cfg)
        try: worker.recover_jobs()
        finally: worker.pool.shutdown()
        assert db.fetchone(cfg.db_path, 'SELECT state FROM jobs WHERE id=?', (queued['id'],))['state'] == 'needs_review'
        assert target.stat().st_ino == inode
        monkeypatch.setattr(sidecars, 'record', original)
        retried = job(client, headers, 'publish_sidecars', [tid], {'plan_id': plan['id']}); drain(cfg)
        assert client.get('/api/jobs/' + retried['id']).json()['state'] == 'succeeded'
        assert target.stat().st_ino == inode  # Reconciled, never rewritten.
        assert sidecars.load_plan(cfg, plan['id'])['results']['Album/cover.jpg']['recovered']


def test_shared_cover_all_owners_immutable_export_and_stale_review(settings):
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        a, _ = generated(cfg, client, headers, 'Album/one.flac')
        b, _ = generated(cfg, client, headers, 'Album/two.flac')
        plan = sidecars.create_plan(cfg, [a, b], cover=True, lrc=True); sidecars.commit(cfg, plan['id'])
        identity = exports.generate(cfg)['export_id']; doc = exports.manifest(cfg, identity)
        assert doc['audio_files'] == 2 and doc['sidecar_files'] == 3 and len(doc['files']) == 5
        cover = next(item for item in doc['files'] if item['kind'] == 'cover')
        assert set(cover['owners']) == {a, b} and cover['usage_provenance']
        playlist = (cfg.state_dir / 'exports' / identity / 'playlist.m3u8').read_text()
        assert playlist.count('#EXTINF:') == 2 and '.lrc' not in playlist and 'cover.jpg' not in playlist
        (cfg.publication_root / 'Ready/Album/cover.jpg').write_bytes(b'external mutation after export')
        snapshot = cfg.state_dir / 'exports' / identity / 'files/Album/cover.jpg'
        response = client.get(f'/api/exports/{identity}/files/Album/cover.jpg')
        assert response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == cover['sha256']
        assert snapshot.read_bytes() == response.content
        checksums = {item['path']: item['sha256'] for item in doc['files']}
        assert client.post('/api/deliveries', json={'export_id': identity, 'device': 'fixture', 'checksums': checksums}, headers=headers).status_code == 201
        del checksums['Album/one.lrc']
        assert client.post('/api/deliveries', json={'export_id': identity, 'device': 'fixture', 'checksums': checksums}, headers=headers).status_code == 409
        (cfg.publication_root / 'Ready/Album/cover.jpg').write_bytes(snapshot.read_bytes())
        accept_publication_review(client, headers, a, profile='relaxed', reason='Owner changed publication profile after sidecar snapshot')
        state = publication.plan_item(cfg, db.fetchone(cfg.db_path, 'SELECT * FROM tracks WHERE id=?', (a,)))['sidecars']
        assert all(item['state'] == 'stale_requires_new_plan' for item in state)
        with pytest.raises(media.MediaError) as stale: exports.generate(cfg)
        assert stale.value.code == 'sidecar_stale_owner'


def test_publish_bytes_reserves_previous_large_backup_not_new_small_content(settings, monkeypatch):
    from contextlib import contextmanager
    cfg = owner(settings); target = cfg.publication_root / 'Ready/cover.jpg'
    target.parent.mkdir(); target.write_bytes(b'x' * (5 * 1024**2))
    sha = media.sha256_file(target); requests = []
    @contextmanager
    def reservation(values):
        requests.extend(values)
        raise media.MediaError('insufficient_space', 'fixture rejects reservation before effects')
        yield
    monkeypatch.setattr(media, '_reserve_filesystem_space', reservation)
    with pytest.raises(media.MediaError): media.publish_bytes(b'small', target, sha, cfg.roots, cfg.publication_root, cfg.state_dir / 'backups/sidecars/test')
    assert requests[1][1] >= target.stat().st_size + 1024**2
    assert media.sha256_file(target) == sha


def test_shared_cover_keeps_restricted_origin_when_original_owner_is_omitted(settings, monkeypatch):
    from test_provider_policy import grant
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        with db.transaction(cfg.db_path) as conn:
            grant(conn, 'coverartarchive', 'cover')
            usage = provider_policy.candidate_usage(conn, {'provider': 'coverartarchive', 'kind': 'cover', 'id': 'fixture-front'})
        source = {'usage': usage}
        a, _ = generated(cfg, client, headers, 'Album/one.flac', provenance={'cover': source})
        b, _ = generated(cfg, client, headers, 'Album/two.flac')
        first = sidecars.create_plan(cfg, [a], cover=True); sidecars.commit(cfg, first['id'])
        second = sidecars.create_plan(cfg, [b], cover=True); sidecars.commit(cfg, second['id'])
        with db.connect(cfg.db_path) as conn:
            registry = sidecars.saved(conn, sidecars.registry_key('Album/cover.jpg'))
        assert registry['usage_provenance'][a + ':cover'] == source
        with db.transaction(cfg.db_path) as conn: grant(conn, 'coverartarchive', 'cover', allow_export=False)
        monkeypatch.setattr(comparison, 'publication_choice', lambda settings, identity: b)
        with pytest.raises(media.MediaError) as denied: exports.generate(cfg)
        assert denied.value.code == 'provider_policy_denied'


def test_matching_unjournaled_destination_is_not_a_successful_commit(settings):
    cfg = owner(settings)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg); tid, _ = generated(cfg, client, headers)
        plan = sidecars.create_plan(cfg, [tid], cover=True)
        raw = sidecars.content(cfg, plan['members'][0], 'cover')
        target = cfg.publication_root / 'Ready/Album/cover.jpg'; target.write_bytes(raw)
        with pytest.raises(media.MediaError) as conflict: sidecars.commit(cfg, plan['id'])
        assert conflict.value.code == 'sidecar_destination_conflict'
        with db.connect(cfg.db_path) as conn: assert sidecars.saved(conn, sidecars.registry_key('Album/cover.jpg')) is None
