"""Durable single scheduler with bounded, concurrent file I/O workers.

Run independently of the API: ``python -m phonoteka.worker``.
The database is a queue, not a message acknowledgement shortcut for file commits.
"""

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from . import db, media
from .config import Settings

LOG = logging.getLogger("phonoteka.worker")
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".ape", ".wv", ".wma", ".webm"}
EXCLUDED_DIRS = {"@eadir", "#recycle", ".trash", ".snapshot", ".phonoteka-staging", "@recycle"}


@contextmanager
def scheduler_guard(state: Path):
    """Second scheduler cannot mark the first scheduler's jobs interrupted."""
    state.mkdir(parents=True, exist_ok=True)
    path = state / "scheduler.lock"
    if path.is_symlink():
        raise RuntimeError("Scheduler lock must not be a symlink")
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another scheduler owns this state directory") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another scheduler owns this state directory") from exc
        yield


def verify_storage(settings: Settings):
    """Check installer attestation; never infer host mapping from container paths."""
    if not settings.writable:
        return
    path = settings.storage_guard_dir / "deployment.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        raise RuntimeError("Verified deployment manifest is required for music writes")
    document = json.loads(path.read_text(encoding="utf-8"))
    expected = {"instance_id": settings.instance_id, "work_root": str(settings.work_root),
                "source_root": str(settings.source_root), "publication_root": str(settings.publication_root)}
    if not document.get("storage_domain") or any(document.get(key) != val for key, val in expected.items()):
        raise RuntimeError("Storage deployment manifest does not match this worker")
    identities = set()
    for path in settings.roots.values():
        if not path.is_dir() or path.is_symlink():
            raise RuntimeError("Storage roots must be existing local directories")
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in identities:
            raise RuntimeError("Storage roots refer to the same physical directory through aliases")
        identities.add(identity)


def root_identity(path: Path):
    if not path.is_dir() or path.is_symlink():
        raise media.MediaError("storage_unavailable", "Work root is unavailable")
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "path": str(path.resolve())}


def enumerate_audio(root: Path):
    """Bounded metadata-only enumeration; errors make the whole scan partial."""
    files, errors = [], []
    def on_error(exc):
        errors.append({"code": "directory_unavailable", "message": str(exc)})
    for folder, dirs, names in os.walk(root, followlinks=False, onerror=on_error):
        dirs[:] = [name for name in dirs if name.lower() not in EXCLUDED_DIRS and not (Path(folder)/name).is_symlink()]
        for name in names:
            path = Path(folder)/name
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                if path.is_symlink():
                    errors.append({"code": "symlink_skipped", "relative_path": str(path.relative_to(root))})
                    continue
                files.append(path.relative_to(root).as_posix())
    return sorted(files), errors


def normalize_metadata(meta: dict):
    result = dict(meta)
    result["lrc"] = result.get("lrc") or ""
    result["lyrics"] = result.get("lyrics") or ""
    return result


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = ThreadPoolExecutor(max_workers=settings.io_threads, thread_name_prefix="music-io")
        self.stopping = False
        self.state = "starting"

    async def io(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, lambda: fn(*args, **kwargs))

    def heartbeat(self):
        db.set_setting(self.settings.db_path, "worker_heartbeat", db.encode({"at": time.time(), "last_seen": db.utcnow(), "state": self.state,
                        "instance_id": self.settings.instance_id, "read_only": not self.settings.writable,
                        "file_workers": self.settings.file_workers, "io_threads": self.settings.io_threads}))

    async def heartbeats(self):
        while not self.stopping:
            await asyncio.to_thread(self.heartbeat)
            await asyncio.sleep(3)

    def claim_job(self):
        with db.transaction(self.settings.db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET state='running',stage=?,heartbeat=?,updated_at=? WHERE id=?", (row["kind"],time.time(),db.utcnow(),row["id"]))
            db.event(conn, "job_started", {"job_id": row["id"]})
            return dict(row)

    def cancelled(self, job_id):
        row = db.fetchone(self.settings.db_path, "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
        return self.stopping or not row or bool(row["cancel_requested"])

    def authorize(self, job):
        if not self.settings.writable:
            raise media.MediaError("read_only", "Music writes are disabled")
        if int(db.setting(self.settings.db_path, "auth_epoch", "0")) != job["auth_epoch"]:
            raise media.MediaError("authorization_revoked", "Owner authorization has been revoked")
        if self.cancelled(job["id"]):
            raise media.MediaError("cancelled", "Job was cancelled before its next effect")

    def item_result(self, job_id, key, state, result):
        with db.transaction(self.settings.db_path) as conn:
            conn.execute("INSERT INTO job_items VALUES(?,?,?,?) ON CONFLICT(job_id,item_key) DO UPDATE SET state=excluded.state,result=excluded.result", (job_id,key,state,db.encode(result)))
            completed = conn.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND state='succeeded'", (job_id,)).fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND state IN ('failed','conflict','needs_review')", (job_id,)).fetchone()[0]
            conn.execute("UPDATE jobs SET completed=?,failed=?,updated_at=?,heartbeat=? WHERE id=?", (completed,failed,db.utcnow(),time.time(),job_id))
            db.event(conn, "job_progress", {"job_id": job_id, "completed": completed, "failed": failed, "track_id": key, "state": state})

    def finish(self, job_id, state, error=None):
        with db.transaction(self.settings.db_path) as conn:
            conn.execute("UPDATE jobs SET state=?,stage=?,error=?,updated_at=? WHERE id=?", (state,state,error,db.utcnow(),job_id))
            db.event(conn, "job_finished", {"job_id": job_id, "state": state})

    async def bounded_items(self, items, fn):
        queue = asyncio.Queue(maxsize=self.settings.file_workers * 2)
        async def consumer():
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    await fn(item)
                finally:
                    queue.task_done()
        async def producer():
            for item in items:
                await queue.put(item)
            for _ in range(self.settings.file_workers):
                await queue.put(None)
        # TaskGroup cancels the producer if any consumer fails, preventing a full
        # queue or unfinished join from hanging shutdown indefinitely.
        async with asyncio.TaskGroup() as group:
            for _ in range(self.settings.file_workers):
                group.create_task(consumer())
            group.create_task(producer())

    def scan_one(self, relative, scan_id):
        path = self.settings.work_root / relative
        before = path.stat()
        meta = normalize_metadata(media.inspect_audio(path, self.settings.roots))
        after = path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino) != (after.st_size,after.st_mtime_ns,after.st_ino):
            raise media.MediaError("file_changing", "File changed during inventory")
        with db.transaction(self.settings.db_path) as conn:
            current = conn.execute("SELECT * FROM tracks WHERE relative_path=? AND is_current=1", (relative,)).fetchone()
            now = db.utcnow()
            if current:
                identity = current["id"]
                old = json.loads(current["metadata"])
                if current["sha256"] == meta["sha256"]:
                    for key in ("audit", "publication"):
                        if key in old:
                            meta[key] = old[key]
                    status = current["status"] if current["status"] not in {"missing", "error"} else "indexed"
                else:
                    # An external path replacement is not proof of physical-copy
                    # identity. Keep old history on its old ID; do not transfer it
                    # to potentially different audio at the same pathname.
                    conn.execute("UPDATE tracks SET is_current=0,status='superseded',updated_at=? WHERE id=?", (now,identity))
                    previous_id, identity = identity, str(uuid.uuid4())
                    conn.execute("INSERT INTO tracks(id,relative_path,sha256,metadata,status,scan_id,created_at,updated_at) VALUES(?,?,?,?,'needs_review',?,?,?)", (identity,relative,meta["sha256"],db.encode(meta),scan_id,now,now))
                    db.event(conn, "file_replaced", {"previous_track_id": previous_id, "track_id": identity})
                    return {"track_id": identity, "relative_path": relative, "identity_changed": True}
                conn.execute("UPDATE tracks SET sha256=?,metadata=?,status=?,scan_id=?,updated_at=? WHERE id=?", (meta["sha256"],db.encode(meta),status,scan_id,now,identity))
            else:
                identity = str(uuid.uuid4())
                conn.execute("INSERT INTO tracks(id,relative_path,sha256,metadata,scan_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (identity,relative,meta["sha256"],db.encode(meta),scan_id,now,now))
        return {"track_id": identity, "relative_path": relative}

    async def scan(self, job):
        identity = await self.io(root_identity, self.settings.work_root)
        previous = db.setting(self.settings.db_path, "work_root_identity")
        if previous and json.loads(previous) != identity:
            raise media.MediaError("root_identity_changed", "Work mount changed; administrator reconciliation is required")
        if not previous:
            db.set_setting(self.settings.db_path, "work_root_identity", db.encode(identity))
        paths, errors = await self.io(enumerate_audio, self.settings.work_root)
        with db.transaction(self.settings.db_path) as conn:
            conn.execute("UPDATE jobs SET total=? WHERE id=?", (len(paths),job["id"]))
        async def one(relative):
            if self.cancelled(job["id"]):
                return
            try:
                result = await self.io(self.scan_one, relative, job["id"])
                self.item_result(job["id"], relative, "succeeded", result)
            except Exception as exc:
                self.item_result(job["id"], relative, "failed", self.failure(exc))
        await self.bounded_items(paths, one)
        stats = db.fetchone(self.settings.db_path, "SELECT * FROM jobs WHERE id=?", (job["id"],))
        final_identity = await self.io(root_identity, self.settings.work_root)
        complete = not errors and not stats["failed"] and not self.cancelled(job["id"]) and final_identity == identity
        if complete:
            with db.transaction(self.settings.db_path) as conn:
                conn.execute("UPDATE tracks SET status='missing',updated_at=? WHERE is_current=1 AND (scan_id IS NULL OR scan_id<>?)", (db.utcnow(),job["id"]))
        db.set_setting(self.settings.db_path, "last_scan", db.encode({"id": job["id"], "state": "complete" if complete else "partial", "errors": errors[:50], "at": db.utcnow()}))
        state = "cancelled_partial" if self.cancelled(job["id"]) and stats["completed"] else "cancelled" if self.cancelled(job["id"]) else "succeeded" if complete else "completed_with_errors"
        self.finish(job["id"], state, "partial_scan" if not complete else None)

    @staticmethod
    def failure(exc):
        if isinstance(exc, media.MediaError):
            return {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
        if isinstance(exc, OSError):
            return {"code": "storage_error", "message": "Ошибка доступа к хранилищу", "retryable": True}
        LOG.exception("Unexpected file operation failure")
        return {"code": "internal_error", "message": "Ошибка обработки файла", "retryable": False}

    def persist_mutation(self, job, item, result, metadata=None):
        track_id = item["track_id"]
        path = self.settings.work_root / item["relative_path"]
        metadata = normalize_metadata(metadata or media.inspect_audio(path, self.settings.roots))
        result = dict(result)
        result["after_sha256"] = result.get("after_sha256", result.get("sha256"))
        with db.transaction(self.settings.db_path) as conn:
            current = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            old = json.loads(current["metadata"])
            if job["kind"] == "publish":
                metadata = old
                metadata["publication"] = {"sha256": item["sha256"], "relative_path": item["relative_path"], "published_at": db.utcnow(), "job_id": job["id"],
                                           "exception": json.loads(job["payload"]).get("params", {}).get("exception_reason")}
                status = current["status"]
            else:
                if old.get("publication"):
                    metadata["publication"] = old["publication"]
                if result.get("audio_identity") and old.get("audit", {}).get("audio_identity") == result["audio_identity"]:
                    metadata["audit"] = {**old["audit"], "sha256": metadata["sha256"], "reused_by_audio_identity": True}
                status = "accepted" if job["kind"] == "apply" else "indexed"
            conn.execute("UPDATE tracks SET sha256=?,metadata=?,status=?,updated_at=? WHERE id=?", (metadata["sha256"],db.encode(metadata),status,db.utcnow(),track_id))
            effect_id = result.get("operation_id", str(uuid.uuid4()))
            conn.execute("INSERT OR IGNORE INTO history VALUES(?,?,?,?,?,?,?,?)", (effect_id,track_id,job["id"],job["kind"],result.get("before_sha256"),result.get("after_sha256"),db.encode(result),db.utcnow()))
            db.event(conn, "file_committed", {"track_id": track_id, "job_id": job["id"], "kind": job["kind"]})

    def process_item(self, job, item):
        path = self.settings.work_root / item["relative_path"]
        current = db.fetchone(self.settings.db_path, "SELECT * FROM tracks WHERE id=? AND is_current=1", (item["track_id"],))
        if not current or current["sha256"] != item["sha256"]:
            raise media.MediaError("conflict", "Input revision is no longer current")
        if job["kind"] == "audit":
            result = media.audit_audio(path, self.settings.roots)
            if result.get("sha256") != item["sha256"]:
                raise media.MediaError("conflict", "File changed before audit")
            result["checked_at"] = db.utcnow()
            with db.transaction(self.settings.db_path) as conn:
                meta = json.loads(current["metadata"])
                meta["audit"] = result
                conn.execute("UPDATE tracks SET metadata=?,status=?,updated_at=? WHERE id=? AND sha256=?", (db.encode(meta),"error" if result["state"] == "failed" else current["status"],db.utcnow(),item["track_id"],item["sha256"]))
            if result["state"] != "passed":
                raise media.MediaError("audit_failed", "Audio did not pass decoding audit")
            return {"audit": result}
        self.authorize(job)
        backup_dir = self.settings.state_dir / "backups" / item["track_id"] / job["id"]
        authorize = lambda: self.authorize(job)
        if job["kind"] == "apply":
            proposal = item["proposal"]
            cover = base64.b64decode(proposal["cover"].split(",", 1)[1], validate=True) if proposal.get("cover") else None
            result = media.apply_changes(path, proposal.get("tags", {}), proposal.get("lrc"), item["sha256"], backup_dir, self.settings.roots, cover, authorize=authorize)
        elif job["kind"] == "rollback":
            history = db.fetchone(self.settings.db_path, "SELECT * FROM history WHERE id=? AND track_id=?", (item["history_id"],item["track_id"]))
            if not history:
                raise media.MediaError("rollback_unavailable", "Rollback history is unavailable")
            previous = json.loads(history["details"])
            result = media.rollback(path, previous["backup_path"], item["sha256"], self.settings.roots,
                                    backup_sha256=history["before_sha256"], backup_dir=backup_dir, authorize=authorize)
        elif job["kind"] == "publish":
            meta = json.loads(current["metadata"])
            params = json.loads(job["payload"]).get("params", {})
            missing = [name for name in ("title", "artist", "album") if not meta.get("tags", {}).get(name)]
            if not meta.get("has_art"):
                missing.append("cover")
            if not meta.get("lrc"):
                missing.append("synced_lyrics")
            if missing and not params.get("allow_incomplete"):
                raise media.MediaError("needs_review", "Publication profile requires: " + ", ".join(missing))
            audit = media.audit_audio(path, self.settings.roots)
            if audit.get("state") != "passed" or audit.get("sha256") != item["sha256"]:
                raise media.MediaError("audit_failed", "Publication requires a current successful audio audit")
            previous_pub = meta.get("publication", {})
            destination = self.settings.publication_root / "Ready" / item["relative_path"]
            if previous_pub.get("sha256") == item["sha256"] and destination.is_file() and media.sha256_file(destination) == item["sha256"]:
                return {"skipped": True, "reason": "already_published", "sha256": item["sha256"]}
            result = media.publish(path, destination, item["sha256"], self.settings.roots, self.settings.publication_root,
                                   expected_destination_sha256=previous_pub.get("sha256"), backup_dir=backup_dir, authorize=authorize)
            result["audit"] = audit
        else:
            raise media.MediaError("unsupported_job", "Unsupported job")
        self.persist_mutation(job, item, result)
        return {key: val for key, val in result.items() if key not in {"backup_path", "journal_path"}}

    async def process(self, job):
        self.state = job["kind"]
        if job["kind"] == "scan":
            return await self.scan(job)
        items = json.loads(job["payload"])["items"]
        async def one(item):
            if self.cancelled(job["id"]):
                return
            key = item["track_id"]
            self.item_result(job["id"], key, "running", {})
            try:
                result = await self.io(self.process_item, job, item)
                self.item_result(job["id"], key, "succeeded", result)
            except Exception as exc:
                result = self.failure(exc)
                journals = self.settings.state_dir / "backups" / key / job["id"]
                has_intent = job["kind"] in {"apply", "rollback", "publish"} and journals.exists() and any(journals.glob("*.json"))
                state = "interrupted" if has_intent else "conflict" if result["code"] in {"conflict", "revision_conflict"} else "needs_review" if result["code"] in {"needs_review", "invalid_lyrics", "unsupported_format", "authorization_revoked"} else "failed"
                if has_intent:
                    with db.transaction(self.settings.db_path) as conn:
                        conn.execute("UPDATE tracks SET status='needs_review' WHERE id=?", (key,))
                    result["recovery_required"] = True
                self.item_result(job["id"], key, state, result)
        await self.bounded_items(items, one)
        stats = db.fetchone(self.settings.db_path, "SELECT * FROM jobs WHERE id=?", (job["id"],))
        interrupted = db.fetchone(self.settings.db_path, "SELECT COUNT(*) AS n FROM job_items WHERE job_id=? AND state='interrupted'", (job["id"],))["n"]
        if interrupted:
            state = "interrupted"
            # Reconciliation happens only at startup while holding both locks.
            # Do not allow a later scan/job to reinterpret an uncertain file first.
            self.stopping = True
        elif self.cancelled(job["id"]):
            state = "cancelled_partial" if stats["completed"] else "cancelled"
        elif stats["failed"]:
            state = "completed_with_errors" if stats["completed"] else "failed"
        else:
            state = "succeeded"
        self.finish(job["id"], state)

    def recover_jobs(self):
        """Mark interrupted before inspecting journals; never replay a mutation."""
        rows = db.fetchall(self.settings.db_path, "SELECT * FROM jobs WHERE state IN ('running','interrupted')")
        for job in rows:
            self.finish(job["id"], "interrupted", "worker_restarted")
            if not self.settings.writable or job["kind"] not in {"apply", "publish", "rollback"}:
                continue
            for item in json.loads(job["payload"]).get("items", []):
                directory = self.settings.state_dir / "backups" / item["track_id"] / job["id"]
                for journal in directory.glob("*.json") if directory.exists() else []:
                    try:
                        known = db.fetchone(self.settings.db_path, "SELECT id FROM history WHERE id=?", (journal.stem,))
                        if known:
                            self.item_result(job["id"], item["track_id"], "succeeded", {"recovered": True, "history_id": known["id"]})
                            continue
                        restored = media.recover(journal, self.settings.roots)
                        if restored.get("state") == "committed":
                            result = {"operation_id": restored["operation_id"], "before_sha256": restored.get("before_sha256"),
                                      "after_sha256": restored["after_sha256"], "sha256": restored["after_sha256"],
                                      "backup_path": restored.get("backup"), "journal_path": str(journal), "state": "committed", "recovered": True}
                            self.persist_mutation(job, item, result)
                            self.item_result(job["id"], item["track_id"], "succeeded", {"recovered": True, "sha256": result["sha256"]})
                    except Exception as exc:
                        self.item_result(job["id"], item["track_id"], "conflict", self.failure(exc))
            totals = db.fetchone(self.settings.db_path, "SELECT total,completed FROM jobs WHERE id=?", (job["id"],))
            if totals["total"] and totals["total"] == totals["completed"]:
                self.finish(job["id"], "succeeded", "recovered_after_restart")

    async def run(self, once=False):
        self.settings.validate()
        db.initialize(self.settings.db_path)
        with ExitStack() as stack:
            stack.enter_context(scheduler_guard(self.settings.state_dir))
            verify_storage(self.settings)
            if self.settings.writable:
                stack.enter_context(media.writer_guard(self.settings.storage_guard_dir, self.settings.instance_id))
            await self.io(self.recover_jobs)
            self.state = "idle"
            self.heartbeat()
            heartbeat_task = asyncio.create_task(self.heartbeats())
            try:
                while not self.stopping:
                    job = self.claim_job()
                    if job:
                        try:
                            await self.process(job)
                        except Exception as exc:
                            failure = self.failure(exc)
                            # A failure while persisting progress may follow a
                            # committed rename. Mutation jobs always reconcile
                            # journals after restart before any replay is possible.
                            state = "interrupted" if job["kind"] in {"apply", "rollback", "publish"} else "failed"
                            self.finish(job["id"], state, failure["code"])
                            if state == "interrupted":
                                # Cancelled asyncio futures do not cancel running
                                # ThreadPoolExecutor writes. Keep the guards until
                                # the finally block drains every physical I/O task.
                                self.stopping = True
                        self.state = "idle"
                    elif once:
                        break
                    else:
                        await asyncio.sleep(0.5)
            finally:
                self.stopping = True
                heartbeat_task.cancel()
                self.state = "stopped"
                # This must precede every fallible database/logging operation.
                # Cancelled asyncio futures can still have live executor writes;
                # drain synchronously while both scheduler/storage guards remain
                # held, with no await/cancellation point before completion.
                self.pool.shutdown(wait=True, cancel_futures=True)
                try:
                    self.heartbeat()
                except Exception:
                    LOG.warning("Could not persist the final worker heartbeat", exc_info=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Drain queued jobs, then stop")
    parser.add_argument("--health", action="store_true", help="Check worker heartbeat")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if args.health:
        heartbeat = json.loads(db.setting(settings.db_path, "worker_heartbeat", "{}"))
        return 0 if time.time()-heartbeat.get("at", 0) < 30 and heartbeat.get("state") != "stopped" else 1
    worker = Worker(settings)
    def stop(*_):
        worker.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    asyncio.run(worker.run(args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
