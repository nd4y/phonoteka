import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid

from fastapi.testclient import TestClient
import pytest

from phonoteka import db, media, security
from phonoteka.app import create_app
from phonoteka.config import Settings
from phonoteka.worker import Worker, scheduler_guard


@pytest.fixture
def settings(tmp_path):
    for name in ("work", "original", "publication", "state", "guard"):
        (tmp_path/name).mkdir()
    password = tmp_path/"password"
    password.write_text("test-strong-owner-password-123", encoding="utf-8")
    config = Settings(state_dir=tmp_path/"state", work_root=tmp_path/"work", source_root=tmp_path/"original",
                      publication_root=tmp_path/"publication", storage_guard_dir=tmp_path/"guard",
                      owner_password_file=password, app_url="https://testserver", instance_id="test-instance")
    (config.storage_guard_dir/"deployment.json").write_text(json.dumps({
        "storage_domain": "test", "instance_id": config.instance_id,
        "work_root": str(config.work_root), "source_root": str(config.source_root), "publication_root": str(config.publication_root),
    }))
    return config


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as instance:
        yield instance


def authenticated(client, settings):
    response = client.post("/api/auth/login", json={"username": "owner", "password": "test-strong-owner-password-123"}, headers={"Origin": settings.app_url})
    assert response.status_code == 200, response.text
    return {"Origin": settings.app_url, "X-CSRF-Token": response.json()["csrf_token"]}


def seed(settings, relative="a.mp3", *, lrc="", art=False):
    file = settings.work_root/relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"not really audio")
    sha = hashlib.sha256(file.read_bytes()).hexdigest()
    identity = str(uuid.uuid4())
    metadata = {"sha256": sha, "tags": {"title": ["Example"], "artist": ["Artist"]}, "lrc": lrc, "format": "mp3", "has_art": art, "write_supported": True}
    with db.transaction(settings.db_path) as conn:
        conn.execute("INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)", (identity,relative,sha,db.encode(metadata),db.utcnow(),db.utcnow()))
    return identity, sha


def job(client, headers, kind, ids=None, params=None, key=None):
    result = client.post("/api/jobs", json={"kind": kind, "track_ids": ids or [], "params": params or {}}, headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())})
    assert result.status_code == 202, result.text
    return result.json()


def drain(settings):
    asyncio.run(Worker(settings).run(once=True))


def accept_publication_review(client, headers, track_id, *, profile='relaxed', reason='Explicit synthetic owner exception without full metadata'):
    """Exercise the actual owner API instead of bypassing the publication gate."""
    url = f'/api/tracks/{track_id}/lyrics-review'
    review = client.get(url).json()
    body = {'base_version': review['version'], 'document_revision': review['document_revision'],
            'profile': profile, 'reason': reason, 'accept_revision': True,
            'instrumental_confirmed': profile == 'instrumental',
            'lines': [{'line_id': line['line_id'], 'words_state': 'accepted', 'timing_state': 'accepted'}
                      for line in review['lines'] if line['timing_valid']],
            'listened_sections': ['start', 'middle', 'end'] if review['lines'] else []}
    response = client.put(url, json=body, headers={**headers, 'If-Match': f'"{review["file_revision"]}"'})
    assert response.status_code == 200, response.text
    return response.json()


def synthetic(settings, ext="flac"):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for integration fixtures")
    path = settings.work_root/("sample."+ext)
    codec = "flac" if ext == "flac" else "libmp3lame"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-metadata", "title=Original", "-metadata", "artist=Artist", "-c:a", codec, str(path)], check=True)
    shutil.copy2(path, settings.source_root/path.name)
    return path


def test_auth_cookie_csrf_and_private_endpoints(client, settings):
    assert client.get("/api/tracks").status_code == 401
    assert client.get("/api/auth/session").json()["authenticated"] is False
    bad_origin = client.post("/api/auth/login", json={"username": "owner", "password": "x"}, headers={"Origin": "https://evil.test"})
    assert bad_origin.status_code == 403
    headers = authenticated(client, settings)
    assert client.cookies.get("__Host-phonoteka")
    assert client.post("/api/jobs", json={"kind": "scan"}, headers={"Idempotency-Key": "no-csrf"}).status_code == 403
    assert client.get("/api/v1/tracks").status_code == 200
    assert client.get("/api/tracks").headers["Cache-Control"] == "private, no-store"
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/tracks").status_code == 401


def test_fingerprint_opt_in_and_preserved_asr(settings, monkeypatch):
    from phonoteka import recognition
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);tid,sha=seed(settings)
        response=client.post('/api/jobs',json={'kind':'recognize','track_ids':[tid]},headers={**headers,'Idempotency-Key':'disabled-fp'})
        assert response.status_code==409
    cfg=replace(settings,external_fingerprints=True)
    monkeypatch.setattr(recognition,'sample',lambda *args:(b'sample',1))
    async def found(*args,input_revision=None):
        assert input_revision==sha
        return {'id':'shazam:1','provider':'shazam','kind':'metadata','tags':{'title':'Actual','artist':'Singer'},'identification':'audio_fingerprint','match':{'state':'fingerprint_match','reasons':[]}}
    monkeypatch.setattr(recognition,'recognize',found)
    with db.transaction(cfg.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',(tid,sha,db.encode({'asr':{'text':'retained'},'candidates':[]}),db.utcnow()))
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg)
        queued=job(client,headers,'recognize',params={'scope':'all'})
        drain(cfg)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['analysis']['asr']['text']=='retained'
        identified=detail['analysis']['candidates'][0]
        assert identified['provider']=='shazam' and identified['identification']=='audio_fingerprint'
        assert identified['transient_only'] and 'tags' not in identified
        assert identified['usage']['allow_cache'] is False and identified['usage']['license_verified'] is False
        assert detail['sha256']==sha and not detail.get('proposal')
        assert client.get('/api/reports/collection').json()['counts']['fingerprint_matches']==1
        pending=client.post('/api/jobs',json={'kind':'recognize','params':{'scope':'pending'}},headers={**headers,'Idempotency-Key':'pending-fp'})
        assert pending.status_code==409 and pending.json()['code']=='nothing_to_process'


def test_password_reset_revokes_session_and_epoch(client, settings):
    authenticated(client, settings)
    epoch = db.setting(settings.db_path, "auth_epoch")
    settings.owner_password_file.write_text("replacement-strong-password-456")
    security.bootstrap(settings)
    assert db.setting(settings.db_path, "auth_epoch") != epoch
    assert client.get("/api/tracks").status_code == 401
    assert "replacement" not in settings.db_path.read_bytes().decode("latin1")


def test_password_verification_has_bounded_memory_concurrency(client, settings):
    assert security.VERIFY_SLOTS.acquire(blocking=False)
    assert security.VERIFY_SLOTS.acquire(blocking=False)
    try:
        response = client.post("/api/auth/login", json={"username": "owner", "password": "test-strong-owner-password-123"}, headers={"Origin": settings.app_url})
        assert response.status_code == 429
        assert response.json()["code"] == "login_busy"
        assert response.headers["Retry-After"] == "2"
    finally:
        security.VERIFY_SLOTS.release()
        security.VERIFY_SLOTS.release()
    authenticated(client, settings)


def test_durable_rate_limit_and_no_password_validation_echo(client, settings):
    payload = {"username": "owner", "password": "wrong-password-12345"}
    for _ in range(8):
        assert client.post("/api/auth/login", json=payload, headers={"Origin": settings.app_url}).status_code == 401
    assert client.post("/api/auth/login", json=payload, headers={"Origin": settings.app_url}).status_code == 429
    invalid = client.post("/api/auth/login", json={**payload, "extra": "secret-material"}, headers={"Origin": settings.app_url})
    assert invalid.status_code == 422
    assert "secret-material" not in invalid.text
    assert "wrong-password" not in invalid.text


def test_idempotency_proposal_cas_and_snapshot(client, settings):
    headers = authenticated(client, settings)
    tid, sha = seed(settings)
    body = {"tags": {"title": "Draft"}, "lrc": "untimed draft"}
    assert client.post(f"/api/tracks/{tid}/proposal", json=body, headers=headers).status_code == 428
    assert client.post(f"/api/tracks/{tid}/proposal", json=body, headers={**headers, "If-Match": "0"*64}).status_code == 412
    proposal = client.post(f"/api/tracks/{tid}/proposal", json=body, headers={**headers, "If-Match": sha})
    assert proposal.status_code == 201
    first = job(client, headers, "scan", key="same-key")
    assert job(client, headers, "scan", key="same-key")["id"] == first["id"]
    changed = client.post("/api/jobs", json={"kind": "audit", "track_ids": [tid]}, headers={**headers, "Idempotency-Key": "same-key"})
    assert changed.status_code == 409
    assert client.post("/api/jobs", json={"kind": "apply", "track_ids": [tid]}, headers={**headers,"Idempotency-Key": "apply"}).json()["code"] == "read_only"
    assert client.get(f"/api/tracks/{tid}").json()["tags"]["title"] == "Example"


def test_proposal_base_retains_cover_and_rejects_stale_draft(client, settings):
    headers = authenticated(client, settings)
    tid, sha = seed(settings)
    h = {**headers, "If-Match": sha}
    cover = "data:image/png;base64,iVBORw0KGgo="
    first = client.post(f"/api/tracks/{tid}/proposal", json={"tags": {"title": "Draft"}, "cover": cover}, headers=h)
    assert first.status_code == 201
    body = {"base_proposal_id": first.json()["id"], "tags": {"album": "Draft album"}}
    second = client.post(f"/api/tracks/{tid}/proposal", json=body, headers=h)
    assert second.status_code == 201
    saved = db.fetchone(settings.db_path, "SELECT payload FROM proposals WHERE id=?", (second.json()["id"],))
    payload = json.loads(saved["payload"])
    assert payload["cover"] == cover
    assert payload["tags"] == {"title": "Draft", "album": "Draft album"}
    assert client.post(f"/api/tracks/{tid}/proposal", json=body, headers=h).status_code == 412


def test_filters_before_pagination_and_publication_revision(client, settings):
    headers = authenticated(client, settings)
    tid, sha = seed(settings, "one.mp3", art=True)
    seed(settings, "two.mp3", lrc="[00:00]One")
    assert client.get("/api/tracks?filter=missing_art&page_size=1").json()["total"] == 1
    assert client.get("/api/tracks?filter=missing_lyrics&page_size=1").json()["total"] == 1
    with db.transaction(settings.db_path) as conn:
        row = conn.execute("SELECT metadata FROM tracks WHERE id=?", (tid,)).fetchone()
        meta = json.loads(row[0])
        meta["publication"] = {"sha256": "old"}
        conn.execute("UPDATE tracks SET metadata=? WHERE id=?", (db.encode(meta),tid))
    assert client.get("/api/summary").json()["published"] == 0
    assert client.get("/api/tracks?filter=published").json()["total"] == 0


def test_original_work_overlap_and_scheduler_duplicate_rejected(settings):
    with pytest.raises(ValueError):
        replace(settings, work_root=settings.source_root/"copy").validate()
    with scheduler_guard(settings.state_dir):
        with pytest.raises(RuntimeError):
            with scheduler_guard(settings.state_dir):
                pass


def test_scan_reads_copy_and_unavailable_root_does_not_mark_missing(client, settings):
    headers = authenticated(client, settings)
    path = synthetic(settings)
    original_sha = media.sha256_file(settings.source_root/path.name)
    first = job(client, headers, "scan")
    drain(settings)
    assert client.get("/api/jobs/"+first["id"]).json()["state"] == "succeeded"
    result = client.get("/api/tracks").json()
    assert result["total"] == 1
    assert media.sha256_file(settings.source_root/path.name) == original_sha
    settings.work_root.rename(settings.work_root.with_name("temporarily-unmounted"))
    second = job(client, headers, "scan")
    drain(settings)
    assert client.get("/api/jobs/"+second["id"]).json()["state"] == "failed"
    assert client.get("/api/tracks").json()["items"][0]["status"] != "missing"


@pytest.mark.skipif(sys.platform != "linux", reason="Durable mutation requires Linux")
def test_manual_roundtrip_recovery_publish_and_rollback(settings, monkeypatch):
    settings = replace(settings, read_only=False, storage_verified=True)
    path = synthetic(settings)
    original_sha = media.sha256_file(path)
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        job(client, headers, "scan")
        drain(settings)
        track = client.get("/api/tracks").json()["items"][0]
        proposal = {"tags": {"title": "Accepted"}, "lrc": "[00:00.000]One\n[00:01.000]Two"}
        response = client.post(f"/api/tracks/{track['id']}/proposal", json=proposal, headers={**headers, "If-Match": track["sha256"]})
        assert response.status_code == 201, response.text
        apply = job(client, headers, "apply", [track["id"]])
        # Simulate a committed file followed by database publication failure.
        original_persist = Worker.persist_mutation
        def crash_after_file(*args, **kwargs):
            raise OSError("simulated database interruption after file commit")
        monkeypatch.setattr(Worker, "persist_mutation", crash_after_file)
        drain(settings)
        interrupted = client.get("/api/jobs/"+apply["id"]).json()
        assert interrupted["state"] == "interrupted", interrupted
        assert media.sha256_file(path) != original_sha
        monkeypatch.setattr(Worker, "persist_mutation", original_persist)
        drain(settings)
        recovered = client.get("/api/jobs/"+apply["id"]).json()
        assert recovered["state"] == "succeeded", recovered
        changed = client.get(f"/api/tracks/{track['id']}").json()
        assert changed["sha256"] == media.sha256_file(path)
        assert len(changed["history"]) == 1
        unreviewed = job(client, headers, 'publish', [track['id']], {'allow_incomplete': True, 'exception_reason': 'Legacy flag must not bypass owner review'})
        drain(settings)
        assert client.get('/api/jobs/' + unreviewed['id']).json()['state'] == 'needs_review'
        assert not (settings.publication_root / 'Ready' / path.name).exists()
        accept_publication_review(client, headers, track['id'], reason='Synthetic acceptance fixture without artwork')
        publication = job(client, headers, "publish", [track["id"]], {"allow_incomplete": True, "exception_reason": "Synthetic acceptance fixture without artwork"})
        drain(settings)
        assert client.get("/api/jobs/"+publication["id"]).json()["state"] == "succeeded"
        assert media.sha256_file(settings.publication_root/"Ready"/path.name) == changed["sha256"]
        published = client.get(f"/api/tracks/{track['id']}").json()['publication']
        assert published['profile'] == 'relaxed' and published['review_version'] == 1
        assert published['lyrics_gate']['state'] == 'passed'
        from phonoteka import exports
        exported = exports.manifest(settings, exports.generate(settings)['export_id'])['files'][0]
        assert exported['publication_profile'] == 'relaxed'
        assert exported['lyrics_review_version'] == 1
        assert exported['publication_exception_reason'] == 'Synthetic acceptance fixture without artwork'
        rollback = job(client, headers, "rollback", [track["id"]])
        drain(settings)
        assert client.get("/api/jobs/"+rollback["id"]).json()["state"] == "succeeded"
        assert media.sha256_file(path) == original_sha
        assert media.sha256_file(settings.source_root/path.name) == original_sha
        assert list((settings.state_dir/"backups"/track["id"]/rollback["id"]).glob("*.json"))


def test_bounded_queue_propagates_consumer_failure(settings):
    async def run():
        worker = Worker(settings)
        async def explode(item):
            raise RuntimeError("consumer failed")
        try:
            with pytest.raises(ExceptionGroup):
                await asyncio.wait_for(worker.bounded_items(range(100), explode), timeout=2)
        finally:
            worker.pool.shutdown()
    asyncio.run(run())


def test_mutation_scheduler_failure_drains_inflight_write_before_exit(client, settings, monkeypatch):
    settings = replace(settings, read_only=False, storage_verified=True)
    one, two = str(uuid.uuid4()), str(uuid.uuid4())
    next_id = str(uuid.uuid4())
    running_id = str(uuid.uuid4())
    now = db.utcnow()
    with db.transaction(settings.db_path) as conn:
        conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,'apply','queued','pending',?,2,?,?,1)", (running_id,db.encode({"items": [{"track_id": one}, {"track_id": two}]}),now,now))
        conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,'scan','queued','pending',?,0,?,?,1)", (next_id,db.encode({"items": []}),now+"1",now+"1"))
    other_started = threading.Event()
    other_finished = threading.Event()
    release = threading.Event()
    original_result = Worker.item_result
    def pretend_write(self, job, item):
        if item["track_id"] == one:
            assert other_started.wait(2)
        else:
            other_started.set()
            assert release.wait(2)
            other_finished.set()
        return {"done": True}
    def fail_progress(self, jid, key, state, result):
        if key == one and state != "running":
            threading.Timer(0.15, release.set).start()
            raise OSError("simulated progress database unavailable")
        return original_result(self, jid, key, state, result)
    monkeypatch.setattr(Worker, "process_item", pretend_write)
    monkeypatch.setattr(Worker, "item_result", fail_progress)
    drain(settings)
    assert other_finished.is_set(), "Executor writes must finish before worker releases its storage guard"
    assert db.fetchone(settings.db_path, "SELECT state FROM jobs WHERE id=?", (running_id,))["state"] == "interrupted"
    assert db.fetchone(settings.db_path, "SELECT state FROM jobs WHERE id=?", (next_id,))["state"] == "queued"


@pytest.mark.skipif(sys.platform != "linux", reason="Storage guard requires Linux")
def test_final_heartbeat_failure_cannot_release_guards_before_executor_drains(settings, monkeypatch):
    settings = replace(settings, read_only=False, storage_verified=True)
    worker = Worker(settings)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    future = None
    checked_shutdown = []
    original_shutdown = worker.pool.shutdown
    original_heartbeat = worker.heartbeat

    def active_io():
        started.set()
        assert release.wait(3)
        finished.set()

    def claim_without_job():
        nonlocal future
        future = worker.pool.submit(active_io)
        assert started.wait(2)
        return None

    def fail_final_heartbeat():
        if worker.state == "stopped":
            raise OSError("simulated database failure in final heartbeat")
        original_heartbeat()

    def verify_guarded_shutdown(*args, **kwargs):
        assert future is not None and not future.done()
        with pytest.raises(RuntimeError):
            with scheduler_guard(settings.state_dir):
                pass
        with pytest.raises(media.MediaError, match="Another worker owns"):
            with media.writer_guard(settings.storage_guard_dir, "competing-writer"):
                pass
        checked_shutdown.append(True)
        release.set()
        original_shutdown(*args, **kwargs)
        assert finished.is_set()

    monkeypatch.setattr(worker, "claim_job", claim_without_job)
    monkeypatch.setattr(worker, "heartbeat", fail_final_heartbeat)
    monkeypatch.setattr(worker.pool, "shutdown", verify_guarded_shutdown)
    try:
        asyncio.run(worker.run(once=True))
        assert checked_shutdown == [True]
        assert finished.is_set()
    finally:
        release.set()
        original_shutdown(wait=True, cancel_futures=True)


def test_cue_inventory_preview_and_write_protection(client, settings):
    headers = authenticated(client, settings)
    path = synthetic(settings)
    original_sha = media.sha256_file(path)
    cue_path = settings.work_root/'album.cue'
    cue_path.write_text('PERFORMER "Artist"\nTITLE "Album"\nFILE "sample.wav" WAVE\nTRACK 01 AUDIO\nTITLE "First"\nINDEX 01 00:00:00\nTRACK 02 AUDIO\nTITLE "Searchable second"\nINDEX 01 00:01:00')
    job(client, headers, 'scan'); drain(settings)
    tracks = client.get('/api/tracks').json()['items']
    album = next(t for t in tracks if t['format'] == 'cue')
    audio = next(t for t in tracks if t['format'] == 'flac')
    assert len(tracks) == 2
    assert album['cue']['errors'] == [] and len(album['cue']['warnings']) == 1
    assert album['cue']['tracks'][1]['duration'] == 1
    assert not album['can_write'] and not audio['can_write']
    assert audio['cue_album_image'] and audio['cue_sheets'][0]['id'] == album['id']
    assert client.get('/api/tracks?filter=cue').json()['total'] == 1
    summary = client.get('/api/summary').json()
    assert summary['cue_sheets'] == 1 and summary['cue_tracks'] == 2
    assert client.get(f"/api/tracks/{album['id']}/audio").status_code == 409
    endpoint = f"/api/tracks/{album['id']}/cue/2/audio"
    response = client.get(endpoint, headers={'Range': 'bytes=0-31'})
    assert response.status_code == 206 and response.content == path.read_bytes()[:32]
    assert client.get(f"/api/tracks/{audio['id']}/cue/1/audio").status_code == 409
    assert client.get(endpoint, headers={'Range': 'bytes=0-1,4-5'}).status_code == 416
    for track in [album, audio]:
        response = client.post(f"/api/tracks/{track['id']}/proposal", json={'tags': {'title': 'Bad'}}, headers={**headers, 'If-Match': '"'+track['sha256']+'"'})
        assert response.status_code == 409, response.text
        for kind in ['apply', 'publish', 'rollback']:
            response = client.post('/api/jobs', json={'kind': kind, 'track_ids': [track['id']]}, headers={**headers, 'Idempotency-Key': str(uuid.uuid4())})
            assert response.status_code == 409, response.text
    assert media.sha256_file(path) == original_sha == media.sha256_file(settings.source_root/path.name)
    assert client.get('/api/tracks?search=Searchable').json()['total'] == 1
    with pytest.raises(media.MediaError) as exc:
        Worker(settings).process_item({'id': 'fixture-cue-guard', 'kind': 'publish'}, {'track_id': audio['id'], 'sha256': audio['sha256'], 'relative_path': audio['relative_path']})
    assert exc.value.code == 'cue_read_only'
    # A damaged sheet cannot unlock a previously known album image.
    valid_cue = cue_path.read_text()
    cue_path.write_text('broken')
    job(client, headers, 'scan'); drain(settings)
    assert client.get(f"/api/tracks/{audio['id']}").json()['can_write'] is False
    cue_path.write_text(valid_cue)
    job(client, headers, 'scan'); drain(settings)
    album = client.get('/api/tracks?filter=cue').json()['items'][0]
    endpoint = f"/api/tracks/{album['id']}/cue/2/audio"
    # A dependency can change without changing the CUE bytes: rescan refreshes it.
    path.rename(path.with_suffix('.away'))
    job(client, headers, 'scan'); drain(settings)
    assert client.get(f"/api/tracks/{album['id']}").json()['status'] == 'needs_review'
    path.with_suffix('.away').rename(path)
    job(client, headers, 'scan'); drain(settings)
    assert client.get(f"/api/tracks/{album['id']}").json()['status'] == 'indexed'
    assert client.get(endpoint).status_code == 200
    client.post('/api/auth/logout', headers=headers)
    assert client.get(endpoint).status_code == 401


def test_analysis_is_read_only_select_requires_current_revision(client, settings, monkeypatch):
    headers=authenticated(client,settings)
    track_id,sha=seed(settings)
    candidate={'id':'fake:1','kind':'metadata','provider':'musicbrainz','tags':{'title':'Proposed'},'match':{'state':'needs_review','reasons':[]}}
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',(track_id,sha,db.encode({'input_sha256':sha,'candidates':[candidate],'sources':[]}),db.utcnow()))
    before=(settings.work_root/'a.mp3').read_bytes()
    response=client.post(f'/api/tracks/{track_id}/analysis/select',json={'candidate_ids':['fake:1']},headers={**headers,'If-Match':'"'+sha+'"'})
    assert response.status_code==201,response.text
    assert (settings.work_root/'a.mp3').read_bytes()==before
    assert client.get(f'/api/tracks/{track_id}').json()['proposal']['tags']['title']=='Proposed'
    stale=client.post(f'/api/tracks/{track_id}/analysis/select',json={'candidate_ids':['fake:1']},headers={**headers,'If-Match':'"'+'0'*64+'"'})
    assert stale.status_code==412
    fabricated=client.post(f'/api/tracks/{track_id}/analysis/select',json={'candidate_ids':['evil']},headers={**headers,'If-Match':'"'+sha+'"'})
    assert fabricated.status_code==422


def test_bad_audio_is_catalogued_and_webm_readable(client,settings):
    headers=authenticated(client,settings)
    (settings.work_root/'broken.webm').write_bytes(b'x')
    path=settings.work_root/'valid.webm'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2','-c:a','libopus',str(path)],check=True)
    scan=job(client,headers,'scan');drain(settings)
    rows=client.get('/api/tracks').json()['items']
    bad=next(x for x in rows if x['filename']=='broken.webm')
    good=next(x for x in rows if x['filename']=='valid.webm')
    assert bad['status']=='error' and bad['input_error'] and not bad['can_write']
    assert good['codec']=='opus' and good['duration']>0
    assert client.get('/api/jobs/'+scan['id']).json()['failed']==1


def test_collection_reports_auth_duplicates_and_csv_formula(client,settings):
    assert client.get('/api/reports/collection').status_code==401
    assert client.get('/api/reports/export/csv').status_code==401
    authenticated(client,settings)
    first,sha=seed(settings,relative='=formula.mp3');second,_=seed(settings,relative='second.mp3')
    with db.transaction(settings.db_path) as conn:
        for identity,number in [(first,'1/3'),(second,'3/3')]:
            row=conn.execute('SELECT metadata FROM tracks WHERE id=?',(identity,)).fetchone()
            meta=json.loads(row['metadata']);meta['tags'].update({'album':['Album'],'tracknumber':[number]})
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),identity))
    report=client.get('/api/reports/collection').json()
    assert report['counts']['files']==2 and len(report['exact_duplicates'])==1
    assert report['albums'][0]['missing_by_tags']=={'1':[2]}
    assert report['albums'][0]['release_completeness']=='unknown'
    csv=client.get('/api/reports/export/csv');assert csv.status_code==200 and "'=formula.mp3" in csv.text
    exported=client.get('/api/reports/export/json').json();assert len(exported['files'])==2
    assert media.sha256_file(settings.work_root/'=formula.mp3')==sha


def test_analysis_plain_lyrics_are_an_untimed_draft(client,settings):
    headers=authenticated(client,settings);identity,sha=seed(settings)
    candidate={'id':'plain:1','kind':'lyrics','provider':'independent_asr','plain_lyrics':'Words without times'}
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',(identity,sha,db.encode({'candidates':[candidate],'sources':[]}),db.utcnow()))
    endpoint=f'/api/tracks/{identity}/analysis/select';headers={**headers,'If-Match':'"'+sha+'"'}
    assert client.post(endpoint,json=[],headers=headers).status_code==422
    assert client.post(endpoint,json={'candidate_ids':['plain:1']},headers=headers).status_code==201
    proposal=client.get(f'/api/tracks/{identity}').json()['proposal']
    assert proposal['lrc']=='Words without times'
    assert not media.parse_lrc(proposal['lrc'])['complete']


def test_slow_inference_does_not_block_ordinary_queue(settings,monkeypatch):
    from phonoteka import inference_client
    settings=replace(settings,inference_url='http://inference:8001')
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);identity,_=seed(settings)
        long_job=job(client,headers,'transcribe',[identity]);ordinary=job(client,headers,'audit',[identity])
        completed=[]
        async def inference(worker,task,item):
            for _ in range(100):
                if completed:return {'draft':True}
                await asyncio.sleep(.01)
            raise AssertionError('Normal queue stalled behind inference')
        def normal(worker,task,item):
            completed.append(task['id']);return {'audit':'fixture'}
        monkeypatch.setattr(inference_client,'process',inference)
        monkeypatch.setattr(Worker,'process_item',normal)
        drain(settings)
        assert completed==[ordinary['id']]
        assert client.get('/api/jobs/'+long_job['id']).json()['state']=='succeeded'
        assert client.get('/api/jobs/'+ordinary['id']).json()['state']=='succeeded'


def test_inference_starts_during_existing_metadata_batch(settings,monkeypatch):
    from phonoteka import inference_client
    settings=replace(settings,inference_url='http://inference:8001',external_metadata=True)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);identity,_=seed(settings)
        first=job(client,headers,'analyze',[identity]);second=job(client,headers,'transcribe',[identity])
        started=[]
        async def analyze(worker,task,item):
            for _ in range(100):
                if started:return {'candidates':[]}
                await asyncio.sleep(.01)
            raise AssertionError('Inference stalled behind metadata queue')
        async def infer(worker,task,item):started.append(task['id']);return {'draft':True}
        monkeypatch.setattr(Worker,'analyze_item',analyze);monkeypatch.setattr(inference_client,'process',infer)
        drain(settings)
        assert started==[second['id']]
        assert all(client.get('/api/jobs/'+j['id']).json()['state']=='succeeded' for j in [first,second])


def test_metadata_refresh_retains_same_revision_asr(client,settings):
    settings=replace(settings,external_metadata=True)
    headers=authenticated(client,settings);identity,sha=seed(settings)
    previous={'input_sha256':sha,'asr':{'text':'independent draft'},'alignment':{'coverage':.5},'candidates':[{'id':'alignment:1','provider':'independent_asr'}]}
    with db.transaction(settings.db_path) as conn:conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',(identity,sha,db.encode(previous),db.utcnow()))
    class Fixture:
        async def analyze(self,*args):return {'input_sha256':sha,'candidates':[],'sources':[],'state':'no_match'}
    task={'id':'fixture','kind':'analyze','auth_epoch':int(db.setting(settings.db_path,'auth_epoch','0'))}
    worker=Worker(settings);worker.providers=Fixture();worker.cancelled=lambda _:False
    try:asyncio.run(worker.analyze_item(task,{'track_id':identity,'sha256':sha,'relative_path':'a.mp3'}))
    finally:worker.pool.shutdown()
    result=json.loads(db.fetchone(settings.db_path,'SELECT payload FROM analyses WHERE track_id=?',(identity,))['payload'])
    assert result['asr']==previous['asr'] and result['alignment']==previous['alignment']
    assert len(result['candidates'])==1
    assert all(result['candidates'][0][key]==value for key,value in previous['candidates'][0].items())
    assert result['candidates'][0]['usage']['origin']=='owner_supplied'
    assert result['candidates'][0]['usage']['license_verified'] is False


def test_alignment_freezes_reference_at_enqueue(settings):
    settings=replace(settings,inference_url='http://inference:8001')
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);identity,sha=seed(settings,lrc='[00:01.00]Original words')
        task=job(client,headers,'align',[identity])
        client.post(f'/api/tracks/{identity}/proposal',json={'tags':{},'lrc':'[00:02.00]Other words'},headers={**headers,'If-Match':'"'+sha+'"'})
        stored=json.loads(db.fetchone(settings.db_path,'SELECT payload FROM jobs WHERE id=?',(task['id'],))['payload'])
        assert stored['items'][0]['reference_text']=='[00:01.00]Original words'


def test_extensionless_and_wrong_suffix_audio_cycle(settings):
    settings=replace(settings,read_only=False,storage_verified=True)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings)
        original=synthetic(settings,'mp3');noext=settings.work_root/'download_123';original.rename(noext)
        disguised=settings.work_root/'audio.dat';shutil.copy2(noext,disguised)
        (settings.work_root/'image.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        (settings.work_root/'.nomedia').write_bytes(b'')
        task=job(client,headers,'scan');drain(settings)
        tracks=client.get('/api/tracks').json()['items'];assert len(tracks)==2
        assert all(t['format']=='mp3' and t['can_write'] for t in tracks)
        track=next(t for t in tracks if t['relative_path']=='download_123')
        assert track['extension_missing']
        response=client.get('/api/tracks/'+track['id']+'/audio',headers={'Range':'bytes=0-31'})
        assert response.status_code==206 and response.headers['content-type']=='audio/mpeg'
        before=media.audio_identity(noext)
        client.post('/api/tracks/'+track['id']+'/proposal',json={'tags':{'title':'Verified name'}},headers={**headers,'If-Match':'"'+track['sha256']+'"'})
        apply=job(client,headers,'apply',[track['id']]);drain(settings)
        assert client.get('/api/jobs/'+apply['id']).json()['state']=='succeeded'
        assert noext.exists() and media.audio_identity(noext)==before
        accept_publication_review(client,headers,track['id'],reason='Controlled fixture without full metadata')
        published=job(client,headers,'publish',[track['id']],params={'allow_incomplete':True,'exception_reason':'Controlled fixture without full metadata'})
        drain(settings);assert client.get('/api/jobs/'+published['id']).json()['state']=='succeeded'
        target=settings.publication_root/'Ready'/'download_123.mp3'
        assert target.exists() and media.sha256_file(target)==media.sha256_file(noext)
        assert client.get('/api/tracks/'+track['id']).json()['publication']['relative_path']=='download_123.mp3'
        assert noext.exists() and not (settings.work_root/'download_123.mp3').exists()


def test_discovery_does_not_mistake_utf16_logs_for_audio():
    from phonoteka.intake import header_is_audio
    for text in ['Exact Audio Copy log file', 'Report from CD ripper', 'Album and artist']:
        assert not header_is_audio(text.encode('utf-16')[:32])
    for header in [bytes.fromhex('fffb9064'), bytes.fromhex('fff15080'), b'ID3\x04'+bytes(28), b'fLaC'+bytes(28)]:
        assert header_is_audio(header)


def test_search_matches_values_not_json_keys_with_unicode_case(client, settings):
    authenticated(client, settings)
    one,_=seed(settings,'one.mp3');two,_=seed(settings,'two.mp3')
    with db.transaction(settings.db_path) as conn:
        for identity,tags in [(one,{'title':['Numb'],'artist':['Linkin Park'],'tracknumber':'13'}),(two,{'title':['ПЕСНЯ %_'],'artist':['Артист'],'tracknumber':'1'})]:
            row=conn.execute('SELECT metadata FROM tracks WHERE id=?',(identity,)).fetchone()
            meta=json.loads(row['metadata']);meta['tags']=tags
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),identity))
    for term,expected in [('numb',one),('песня',two),('%_',two),('LINKIN',one)]:
        result=client.get('/api/tracks',params={'search':term}).json()
        assert result['total']==1 and result['items'][0]['id']==expected
    assert client.get('/api/tracks',params={'search':'tracknumber'}).json()['total']==0


def test_logout_all_revokes_two_sessions_without_revoking_approved_jobs(client, settings):
    headers = authenticated(client, settings)
    track_id, _ = seed(settings)
    pending = job(client, headers, "audit", [track_id])
    before_epoch = db.setting(settings.db_path, 'auth_epoch', '0')
    track_before = dict(db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,)))
    with TestClient(create_app(settings), base_url=settings.app_url) as second:
        other_headers = authenticated(second, settings)
        assert client.get('/api/tracks').status_code == 200
        assert second.get('/api/tracks').status_code == 200
        denied = client.post('/api/auth/logout-all', headers={'Origin':settings.app_url})
        assert denied.status_code == 403
        assert second.get('/api/tracks').status_code == 200
        response = client.post('/api/auth/logout-all', headers=headers)
        assert response.status_code == 200
        assert response.json()['authenticated'] is False
        assert 'Max-Age=0' in response.headers['set-cookie']
        assert client.get('/api/tracks').status_code == 401
        assert second.get('/api/tracks').status_code == 401
        assert second.post('/api/auth/logout-all', headers=other_headers).status_code == 401
    assert db.setting(settings.db_path, 'auth_epoch', '0') == before_epoch
    assert db.fetchone(settings.db_path, 'SELECT state FROM jobs WHERE id=?', (pending['id'],))['state'] == 'queued'
    assert dict(db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))) == track_before
    event = db.fetchone(settings.db_path, "SELECT * FROM events WHERE kind='owner_sessions_revoked'")
    assert json.loads(event['payload']) == {'sessions':2}
