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


def test_password_reset_revokes_session_and_epoch(client, settings):
    authenticated(client, settings)
    epoch = db.setting(settings.db_path, "auth_epoch")
    settings.owner_password_file.write_text("replacement-strong-password-456")
    security.bootstrap(settings)
    assert db.setting(settings.db_path, "auth_epoch") != epoch
    assert client.get("/api/tracks").status_code == 401
    assert "replacement" not in settings.db_path.read_bytes().decode("latin1")


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
        publication = job(client, headers, "publish", [track["id"]], {"allow_incomplete": True, "exception_reason": "Synthetic acceptance fixture without artwork"})
        drain(settings)
        assert client.get("/api/jobs/"+publication["id"]).json()["state"] == "succeeded"
        assert media.sha256_file(settings.publication_root/"Ready"/path.name) == changed["sha256"]
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
