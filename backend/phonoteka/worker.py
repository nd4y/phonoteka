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
import sqlite3
import sys
import threading
import time
import uuid

from . import db, media, cue, cue_export, inference_client, intake, recognition, loudness, scheduling, exports, youtube, field_policy, provider_policy, provider_attempts
from .providers import Providers, ProviderError
from .config import Settings
from .telemetry import Metrics, configure_logging, context, log, opaque_id, bounded, KINDS, worker_server

LOG = logging.getLogger("phonoteka.worker")
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".ape", ".wv", ".wma", ".webm", ".cue", ".m4b", ".oga", ".aif", ".mp4", ".mka", ".mkv", ".dsf", ".dff", ".ac3", ".wave", ".bin"}
EXCLUDED_DIRS = {"@eadir", "#recycle", ".trash", ".snapshot", ".phonoteka-staging", "@recycle"}
DB_BUSY_RETRY_SECONDS = .5


def sqlite_busy(exc):
    """Extended BUSY/LOCKED codes are contention; other DB errors are fatal."""
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(exc, sqlite3.OperationalError) and isinstance(code, int) and (
        code & 0xff
    ) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


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
            candidate=path.suffix.lower() in AUDIO_EXTENSIONS
            if not candidate and not path.is_symlink():
                try:
                    with media._file(path) as stream:candidate=intake.header_is_audio(stream.read(32))
                except (OSError,media.MediaError) as exc:
                    errors.append({'code':'discovery_unreadable','relative_path':str(path.relative_to(root))})
                    continue
            if candidate:
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
        self.providers = None
        self.inference_task = None
        self.normal_task = None
        self.metrics = Metrics('worker')
        self.item_started = {}
        # Keep a group's file rename and subsequent catalog/history commit in
        # one lane. Other workers cannot observe our own half-persisted SHA.
        # Storage/scheduler guards already restrict this domain to one Worker.
        self.album_gain_mutation_lock = threading.RLock()

    async def io(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, lambda: fn(*args, **kwargs))

    def heartbeat(self):
        db.set_setting(self.settings.db_path, "worker_heartbeat", db.encode({"at": time.time(), "last_seen": db.utcnow(), "state": self.state,
                        "instance_id": self.settings.instance_id, "read_only": not self.settings.writable,
                        "file_workers": self.settings.file_workers, "io_threads": self.settings.io_threads}))

    async def heartbeats(self):
        while not self.stopping:
            delay = 3
            try:
                await asyncio.to_thread(self.heartbeat)
            except sqlite3.OperationalError as exc:
                if not sqlite_busy(exc):
                    raise
                log('worker_database_busy', logging.WARNING, stage='heartbeat')
                delay = DB_BUSY_RETRY_SECONDS
            await asyncio.sleep(delay)

    def claim_job(self):
        with db.transaction(self.settings.db_path) as conn:
            busy = bool(self.inference_task and not self.inference_task.done())
            ordinary_busy = bool(self.normal_task and not self.normal_task.done())
            row = conn.execute("SELECT * FROM jobs WHERE state='queued' AND COALESCE(json_extract(payload,'$.retry_at'),0)<=? AND (?=0 OR kind NOT IN ('transcribe','align')) AND (?=0 OR kind IN ('transcribe','align')) ORDER BY created_at,id LIMIT 1",(time.time(),int(busy),int(ordinary_busy))).fetchone()
            if not row:
                return None
            conn.execute("UPDATE jobs SET state='running',stage=?,heartbeat=?,updated_at=? WHERE id=?", (row["kind"],time.time(),db.utcnow(),row["id"]))
            db.event(conn, "job_started", {"job_id": row["id"]})
            return dict(row)

    def job_control(self, job_id):
        # Do not transfer/decode the full immutable 10,000-item payload for
        # every admission check. Only these mutable controls leave SQLite.
        return db.fetchone(self.settings.db_path, """SELECT cancel_requested,auth_epoch,
            json_extract(payload,'$.pause_requested') AS pause_requested,
            json_extract(payload,'$.auth_token_id') AS auth_token_id
            FROM jobs WHERE id=?""", (job_id,))

    def control_cancelled(self, row):
        if self.stopping or not row or bool(row['cancel_requested']):return True
        epoch=int(db.setting(self.settings.db_path,'auth_epoch','0'))
        if row['auth_epoch'] != epoch:return True
        token_id=row['auth_token_id']
        if token_id:
            token=db.fetchone(self.settings.db_path,'SELECT revoked,expires,auth_epoch FROM api_tokens WHERE id=?',(token_id,))
            return not token or token['revoked'] or token['expires']<=time.time() or token['auth_epoch']!=epoch
        return False

    def cancelled(self, job_id):
        return self.stopping or self.control_cancelled(self.job_control(job_id))

    def authorize(self, job):
        if db.setting(self.settings.db_path,'recovery_required','false')=='true':
            raise media.MediaError('recovery_required','Сначала завершите сверку восстановленного состояния')
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
            kind = conn.execute('SELECT kind FROM jobs WHERE id=?',(job_id,)).fetchone()['kind']
        if state == 'running':
            self.item_started[job_id,key] = time.perf_counter()
        else:
            kind = bounded(kind,KINDS)
            self.metrics.items.labels(kind,bounded(state,{'succeeded','failed','needs_review','conflict','interrupted','deferred'})).inc()
            started = self.item_started.pop((job_id,key),None)
            duration = time.perf_counter()-started if started is not None else None
            if duration is not None: self.metrics.item_duration.labels(kind).observe(duration)
            log('item_finished',logging.WARNING if state in {'failed','conflict','interrupted','needs_review','deferred'} else logging.INFO,
                job_id=job_id,track_id=opaque_id(key),kind=kind,state=state,completed=completed,failed=failed,
                duration_seconds=round(duration,6) if duration is not None else None,
                code=bounded(result.get('code'),{'conflict','revision_conflict','needs_review','invalid_lyrics','unsupported_format','authorization_revoked','provider_unavailable','rate_limited','budget_exhausted','cancelled','storage_unavailable','decode_failed','internal_error','recognition_attempt_budget_exhausted','recognition_identity_required'}) if result.get('code') else None)

    def item_action(self,job,item,action):
        with db.transaction(self.settings.db_path) as conn:
            db.event(conn,'job_action',{'job_id':job['id'],'track_id':item['track_id'],'action':action})
        log('item_action',job_id=job['id'],track_id=opaque_id(item['track_id']),kind=job['kind'],action=action)

    def finish(self, job_id, state, error=None):
        with db.transaction(self.settings.db_path) as conn:
            conn.execute("UPDATE jobs SET state=?,stage=?,error=?,updated_at=? WHERE id=?", (state,state,error,db.utcnow(),job_id))
            db.event(conn, "job_finished", {"job_id": job_id, "state": state})
        log('job_finished',logging.ERROR if state in {'failed','interrupted'} else logging.INFO,job_id=job_id,state=state)

    async def bounded_items(self, items, fn, *, should_stop=None):
        queue = asyncio.Queue(maxsize=self.settings.file_workers * 2)
        def stopped():
            return bool(should_stop and should_stop())
        async def consumer():
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    if not stopped():
                        await fn(item)
                finally:
                    queue.task_done()
        async def producer():
            source=iter(items)
            while not stopped():
                try:item=next(source)
                except StopIteration:break
                await queue.put(item)
            # Control requests stop admission and discard only the bounded
            # pending queue. Already started fn calls finish normally; never
            # cancel their asyncio futures or the executor's file operations.
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
        try:
            inspected = cue.inspect_cue(path, self.settings.work_root) if path.suffix.lower() == '.cue' else media.inspect_audio(path, self.settings.roots)
        except media.MediaError as exc:
            if exc.code not in {'invalid_audio','unsupported_format'}: raise
            inspected = media.fallback_inventory(path, self.settings.roots, exc)
        meta = normalize_metadata(inspected)
        if meta.get('format')!='cue' and not meta.get('input_error'):
            meta['youtube']=youtube.merge(youtube.discover(path,self.settings,meta))
        after = path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino) != (after.st_size,after.st_mtime_ns,after.st_ino):
            raise media.MediaError("file_changing", "File changed during inventory")
        # inspect_audio/fallback/CUE inventory performed a complete SHA-256 read.
        # Publish this timestamp only after the inventory's stability check; an
        # old record's date is never carried onto an external replacement.
        meta['hash_verified_at'] = db.utcnow()
        with db.transaction(self.settings.db_path) as conn:
            current = conn.execute("SELECT * FROM tracks WHERE relative_path=? AND is_current=1", (relative,)).fetchone()
            now = db.utcnow()
            if current:
                identity = current["id"]
                old = json.loads(current["metadata"])
                for key in ("cue_sheets", "cue_album_image"):
                    if key in old:
                        meta[key] = old[key]
                if current["sha256"] == meta["sha256"]:
                    for key in ("audit", "publication", "cue_sheets", "cue_album_image", "derived_from", "cue_export", "audio_export", "usage_provenance", "preferred_lyrics_slot", "lyrics_selection_empty", "preferred_cover", "artwork_transform"):
                        if key in old:
                            meta[key] = old[key]
                    if old.get('preferred_lyrics_slot') or old.get('lyrics_selection_empty'):
                        meta['lrc'], meta['lyrics'] = old.get('lrc', ''), old.get('lyrics', '')
                    if 'youtube' in meta:
                        if old.get('derived_from'):
                            meta['youtube']=youtube.discover(path,self.settings,old)
                        meta['youtube']=youtube.merge(meta['youtube'],old.get('youtube'))
                    status = current["status"] if current["status"] not in {"missing", "error"} else "indexed"
                    if meta.get("input_error"): status = "error"
                else:
                    # An external path replacement is not proof of physical-copy
                    # identity. Keep old history on its old ID; do not transfer it
                    # to potentially different audio at the same pathname.
                    conn.execute("UPDATE tracks SET is_current=0,status='superseded',updated_at=? WHERE id=?", (now,identity))
                    previous_id, identity = identity, str(uuid.uuid4())
                    conn.execute("INSERT INTO tracks(id,relative_path,sha256,metadata,status,scan_id,created_at,updated_at) VALUES(?,?,?,?,'needs_review',?,?,?)", (identity,relative,meta["sha256"],db.encode(meta),scan_id,now,now))
                    db.event(conn, "file_replaced", {"previous_track_id": previous_id, "track_id": identity})
                    if meta.get('input_error'): conn.execute("UPDATE tracks SET status='error' WHERE id=?",(identity,))
                    return {"track_id": identity, "relative_path": relative, "identity_changed": True, **({'input_error':meta['input_error']} if meta.get('input_error') else {})}
                if meta.get('format') == 'cue':
                    status = 'needs_review' if meta.get('cue', {}).get('errors') else 'indexed'
                conn.execute("UPDATE tracks SET sha256=?,metadata=?,status=?,scan_id=?,updated_at=? WHERE id=?", (meta["sha256"],db.encode(meta),status,scan_id,now,identity))
            else:
                identity = str(uuid.uuid4())
                conn.execute("INSERT INTO tracks(id,relative_path,sha256,metadata,scan_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (identity,relative,meta["sha256"],db.encode(meta),scan_id,now,now))
                if meta.get('cue', {}).get('errors'):
                    conn.execute("UPDATE tracks SET status='needs_review' WHERE id=?", (identity,))
            if meta.get('input_error'):
                conn.execute("UPDATE tracks SET status='error' WHERE id=?", (identity,))
        return {"track_id": identity, "relative_path": relative, **({'input_error':meta['input_error']} if meta.get('input_error') else {})}

    def link_cue_sheets(self):
        """Mark album images explicitly; individual CUE songs never write their tags."""
        with db.transaction(self.settings.db_path) as conn:
            rows = conn.execute("SELECT id,relative_path,metadata FROM tracks WHERE is_current=1 AND status<>'missing'").fetchall()
            links = {}
            uncertain = any(json.loads(row['metadata']).get('cue', {}).get('errors') for row in rows)
            for row in rows:
                meta = json.loads(row['metadata'])
                document = meta.get('cue', {})
                counts = {}
                for track in document.get('tracks', []):
                    source = document['files'][track['file_index']]
                    if source.get('relative_path'):
                        path = source['relative_path']
                        counts[path] = counts.get(path, 0) + 1
                for path, count in counts.items():
                    links.setdefault(path, []).append({'id': row['id'], 'title': document.get('title') or Path(row['relative_path']).stem, 'track_count': count})
            for row in rows:
                meta = json.loads(row['metadata'])
                if meta.get('format') == 'cue':
                    continue
                # A damaged sheet must not silently unlock a known album image.
                if uncertain and meta.get('cue_album_image') and row['relative_path'] not in links:
                    continue
                meta['cue_sheets'] = links.get(row['relative_path'], [])
                meta['cue_album_image'] = any(x['track_count'] > 1 for x in meta['cue_sheets'])
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), row['id']))

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
        halted=False
        def stop_admission():
            nonlocal halted
            halted=halted or self.cancelled(job['id'])
            return halted
        async def one(relative):
            if stop_admission():
                return
            try:
                result = await self.io(self.scan_one, relative, job["id"])
                self.item_result(job["id"], relative, "failed" if result.get("input_error") else "succeeded", result)
            except Exception as exc:
                self.item_result(job["id"], relative, "failed", self.failure(exc))
        await self.bounded_items(paths, one, should_stop=stop_admission)
        stats = db.fetchone(self.settings.db_path, "SELECT * FROM jobs WHERE id=?", (job["id"],))
        final_identity = await self.io(root_identity, self.settings.work_root)
        complete = not errors and not stats["failed"] and not self.cancelled(job["id"]) and final_identity == identity
        if complete:
            with db.transaction(self.settings.db_path) as conn:
                conn.execute("UPDATE tracks SET status='missing',updated_at=? WHERE is_current=1 AND (scan_id IS NULL OR scan_id<>?)", (db.utcnow(),job["id"]))
        await self.io(self.link_cue_sheets)
        db.set_setting(self.settings.db_path, "last_scan", db.encode({"id": job["id"], "state": "complete" if complete else "partial", "errors": errors[:50], "at": db.utcnow()}))
        state = "cancelled_partial" if self.cancelled(job["id"]) and stats["completed"] else "cancelled" if self.cancelled(job["id"]) else "succeeded" if complete else "completed_with_errors"
        self.finish(job["id"], state, "partial_scan" if not complete else None)

    @staticmethod
    def failure(exc):
        if isinstance(exc, media.MediaError):
            return {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
        if isinstance(exc, OSError):
            return {"code": "storage_error", "message": "Ошибка доступа к хранилищу", "retryable": True}
        LOG.exception('Unexpected file operation failure',extra={'event':'file_operation_failed'})
        return {"code": "internal_error", "message": "Ошибка обработки файла", "retryable": False}

    def persist_mutation(self, job, item, result, metadata=None):
        if job['kind'] in {'publish', 'rollback_ready'}:
            from . import publication
            return publication.persist(self.settings, job, item, result)
        track_id = item["track_id"]
        path = self.settings.work_root / item["relative_path"]
        metadata = normalize_metadata(metadata or media.inspect_audio(path, self.settings.roots))
        from . import media_slots
        metadata['hash_verified_at'] = db.utcnow()
        result = dict(result)
        result["after_sha256"] = result.get("after_sha256", result.get("sha256"))
        with db.transaction(self.settings.db_path) as conn:
            current = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            old = json.loads(current["metadata"])
            result['canonical_before'] = media_slots.canonical_snapshot(old)
            if old.get('artwork_transform'): result['artwork_transform_before'] = old['artwork_transform']
            provenance=old.get('usage_provenance',{})
            if job['kind']=='apply':provenance={**provenance,**item['proposal'].get('field_sources',{})}
            elif job['kind']=='rollback':
                previous_history=conn.execute('SELECT details FROM history WHERE id=? AND track_id=?',(item['history_id'],track_id)).fetchone()
                if previous_history:
                    previous_details = json.loads(previous_history['details'])
                    provenance = previous_details.get('usage_before',provenance)
                    metadata = media_slots.restore_canonical(path, metadata, previous_details.get('canonical_before'))
                    if previous_details.get('artwork_transform_before'): metadata['artwork_transform'] = previous_details['artwork_transform_before']
            if provenance:metadata['usage_provenance']=provenance
            if job['kind'] == 'apply':
                metadata = media_slots.committed_metadata(old, metadata, item['proposal'], result)
            result['usage_before']=old.get('usage_provenance',{})
            result['usage_after']=provenance
            for field in ('derived_from','cue_sheets','cue_album_image','youtube'):
                if field in old: metadata[field]=old[field]
            if old.get("publication"):
                metadata["publication"] = old["publication"]
            if result.get("audio_identity") and old.get("audit", {}).get("audio_identity") == result["audio_identity"]:
                metadata["audit"] = {**old["audit"], "sha256": metadata["sha256"], "reused_by_audio_identity": True}
            status = "accepted" if job["kind"] == "apply" else "indexed"
            from . import lyrics_document
            lyrics_document.committed(conn, current, metadata, item.get('proposal', {}) if job['kind'] == 'apply' else {})
            conn.execute("UPDATE tracks SET sha256=?,metadata=?,status=?,updated_at=? WHERE id=?", (metadata["sha256"],db.encode(metadata),status,db.utcnow(),track_id))
            effect_id = result.get("operation_id", str(uuid.uuid4()))
            conn.execute("INSERT OR IGNORE INTO history VALUES(?,?,?,?,?,?,?,?)", (effect_id,track_id,job["id"],job["kind"],result.get("before_sha256"),result.get("after_sha256"),db.encode(result),db.utcnow()))
            db.event(conn, "file_committed", {"track_id": track_id, "job_id": job["id"], "kind": job["kind"]})

    def process_item(self, job, item):
        from . import album_gain_policy
        if job['kind'] == 'apply' and album_gain_policy.depends_on_group(item.get('proposal', {})):
            with self.album_gain_mutation_lock:
                return self._process_item(job, item)
        return self._process_item(job, item)

    def _process_item(self, job, item):
        from . import publication
        self.item_action(job,item,'preflight')
        path = self.settings.work_root / item["relative_path"]
        current = db.fetchone(self.settings.db_path, "SELECT * FROM tracks WHERE id=? AND is_current=1", (item["track_id"],))
        if not current or current["sha256"] != item["sha256"]:
            raise media.MediaError("conflict", "Input revision is no longer current")
        meta = json.loads(current['metadata'])
        if job['kind'] == 'rollback_ready':
            from . import publication
            return publication.rollback(self.settings, job, item, lambda: self.authorize(job))
        if job['kind'] in {'publish','extract_cue','normalize_audio'}:
            provider_policy.require_track(self.settings,current,'export')
        if job['kind'] in {'extract_cue','normalize_audio'}:
            self.item_action(job,item,'cue_extract' if job['kind']=='extract_cue' else 'normalize')
            self.authorize(job)
            from . import derivatives
            exporter=cue_export if job['kind']=='extract_cue' else derivatives
            result=exporter.export(path,meta,self.settings,lambda:self.authorize(job))
            derived=[]
            for entry in result['files']:
                indexed=self.scan_one(entry['relative_path'],None)
                with db.transaction(self.settings.db_path) as conn:
                    row=conn.execute('SELECT metadata FROM tracks WHERE id=?',(indexed['track_id'],)).fetchone()
                    data=json.loads(row['metadata']);data['derived_from']={**entry,'parent_track_id':item['track_id'],'parent_sha256':item['sha256'],'export_identity':result['identity']}
                    if job['kind']=='extract_cue':data['derived_from'].update(sheet_id=item['track_id'],sheet_sha256=item['sha256'])
                    conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(data),indexed['track_id']))
                derived.append(indexed['track_id'])
            with db.transaction(self.settings.db_path) as conn:
                meta['cue_export' if job['kind']=='extract_cue' else 'audio_export']={'identity':result['identity'],'track_ids':derived,'count':len(derived),'gap_policy':result.get('gap_policy')}
                conn.execute('UPDATE tracks SET metadata=?,updated_at=? WHERE id=? AND sha256=?',(db.encode(meta),db.utcnow(),item['track_id'],item['sha256']))
                db.event(conn,'cue_extracted' if job['kind']=='extract_cue' else 'audio_normalized',{'track_id':item['track_id'],'count':len(derived),'export_identity':result['identity']})
            return {'state':'succeeded','derived_track_ids':derived,'reused':result.get('reused',False),'identity':result['identity']}
        if meta.get('format') == 'cue' or (meta.get('cue_album_image') and job['kind'] not in {'audit','analyze'}):
            raise media.MediaError('cue_read_only', 'CUE и альбомные образы открыты только для чтения')
        if job['kind'] == 'publish':
            from . import lyrics_review
            lyrics_review.assert_publication_frozen(self.settings, current, item.get('publication_review'))
        if job["kind"] == "audit":
            self.item_action(job,item,'decode')
            result = media.audit_audio(path, self.settings.roots)
            if result.get("sha256") != item["sha256"]:
                raise media.MediaError("conflict", "File changed before audit")
            result["checked_at"] = db.utcnow()
            with db.transaction(self.settings.db_path) as conn:
                meta = json.loads(current["metadata"])
                meta["audit"] = result
                if result.get('hash_verified_at'):
                    meta['hash_verified_at'] = result['hash_verified_at']
                conn.execute("UPDATE tracks SET metadata=?,status=?,updated_at=? WHERE id=? AND sha256=?", (db.encode(meta),"error" if result["state"] == "failed" else current["status"],db.utcnow(),item["track_id"],item["sha256"]))
            if result["state"] != "passed":
                raise media.MediaError("audit_failed", "Audio did not pass decoding audit")
            return {"audit": result}
        if job['kind']=='replaygain':
            candidate=loudness.analyze(path,self.settings.roots,item['sha256'])
            if self.cancelled(job['id']) or int(db.setting(self.settings.db_path,'auth_epoch','0'))!=job['auth_epoch']:
                raise media.MediaError('authorization_revoked','Analysis authorization revoked')
            with db.transaction(self.settings.db_path) as conn:
                revision=conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1',(item['track_id'],)).fetchone()
                if not revision or revision['sha256']!=item['sha256']:raise media.MediaError('conflict','File revision changed')
                previous=conn.execute('SELECT * FROM analyses WHERE track_id=?',(item['track_id'],)).fetchone()
                data=json.loads(previous['payload']) if previous and previous['input_sha256']==item['sha256'] else {'input_sha256':item['sha256'],'candidates':[],'sources':[],'query_source':'local_audio','state':'candidates'}
                data['candidates']=[c for c in data['candidates'] if c.get('provider')!='local_replaygain']+[candidate]
                data['sources']=[s for s in data['sources'] if s.get('provider')!='local_replaygain']+[{'provider':'local_replaygain','state':'candidate'}]
                data['state']='candidates'
                conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',(item['track_id'],item['sha256'],db.encode(data),db.utcnow()))
                db.event(conn,'loudness_measured',{'track_id':item['track_id'],'sha256':item['sha256']})
            return {'state':'needs_review','tags':candidate['tags'],'audio_unchanged':True}
        self.authorize(job)
        provider_policy.require_track(self.settings,current,'cache')
        backup_dir = self.settings.state_dir / "backups" / item["track_id"] / job["id"]
        commit_announced=False
        def authorize():
            nonlocal commit_announced
            self.authorize(job)
            provider_policy.require_track(self.settings,current,'cache')
            if job['kind']=='apply':
                from . import automatic_quality
                from . import album_gain_policy
                automatic_quality.assert_frozen(self.settings, current, item, job)
                with db.connect(self.settings.db_path) as connection:
                    field_policy.assert_apply(self.settings,current,item['proposal'],conn=connection)
                    provider_policy.require_payload(connection,current,item['proposal'],'embed')
                    album_gain_policy.payload(self.settings,connection,current,item['proposal'],verify_files=True)
            elif job['kind']=='publish':
                from . import comparison, lyrics_review
                fresh = db.fetchone(self.settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],))
                if not fresh or fresh['sha256'] != item['sha256']:
                    raise media.MediaError('conflict', 'Publication input revision changed before commit')
                provider_policy.require_track(self.settings,fresh,'export')
                comparison.assert_publish_allowed(self.settings,item['track_id'])
                lyrics_review.assert_publish_allowed(self.settings,fresh,audit=audit,frozen=item.get('publication_review'))
                publication.assert_target(self.settings, fresh, item.get('publication_target'))
            if not commit_announced:
                self.item_action(job,item,'commit');commit_announced=True
        if job["kind"] == "apply":
            from . import automatic_quality
            automatic_quality.assert_frozen(self.settings, current, item, job)
            self.item_action(job,item,'write_tags')
            proposal = item["proposal"]
            field_policy.assert_apply(self.settings, current, proposal)
            with db.connect(self.settings.db_path) as connection:
                provider_policy.require_payload(connection,current,proposal,'embed')
                from . import album_gain_policy
                album_gain_policy.payload(self.settings,connection,current,proposal,verify_files=True)
            cover = base64.b64decode(proposal["cover"].split(",", 1)[1], validate=True) if proposal.get("cover") else None
            result = media.apply_changes(path, proposal.get("tags", {}), proposal.get("lrc"), item["sha256"], backup_dir, self.settings.roots, cover, authorize=authorize, slot_selection=proposal.get('slot_selection'))
            mode = item.get('acceptance_mode', 'manual')
            result.update(acceptance_mode=mode, automatic_acceptance=mode == 'automatic')
            if mode == 'automatic':
                result['automatic_quality'] = {**item['automatic_quality'], 'state': 'passed'}
        elif job["kind"] == "rollback":
            self.item_action(job,item,'rollback')
            history = db.fetchone(self.settings.db_path, "SELECT * FROM history WHERE id=? AND track_id=?", (item["history_id"],item["track_id"]))
            if not history:
                raise media.MediaError("rollback_unavailable", "Rollback history is unavailable")
            previous = json.loads(history["details"])
            with db.connect(self.settings.db_path) as connection:
                provider_policy.require_sources(connection,provider_policy.references({'field_sources':previous.get('usage_before',{})}),'embed')
            result = media.rollback(path, previous["backup_path"], item["sha256"], self.settings.roots,
                                    backup_sha256=history["before_sha256"], backup_dir=backup_dir, authorize=authorize)
        elif job["kind"] == "publish":
            self.item_action(job,item,'publication')
            from . import comparison, lyrics_review
            comparison.assert_publish_allowed(self.settings, item['track_id'])
            meta = json.loads(current["metadata"])
            audit = media.audit_audio(path, self.settings.roots)
            if audit.get("state") != "passed" or audit.get("sha256") != item["sha256"]:
                raise media.MediaError("audit_failed", "Publication requires a current successful audio audit")
            gate = lyrics_review.assert_publish_allowed(self.settings, current, audit=audit, frozen=item.get('publication_review'))
            previous_pub = meta.get("publication", {})
            publication.assert_target(self.settings,current,item.get('publication_target'))
            destination = self.settings.publication_root / "Ready" / intake.publication_relative(item["relative_path"],meta)
            if previous_pub.get("sha256") == item["sha256"] and previous_pub.get('profile') == gate['profile'] and previous_pub.get('review_version') == gate['review_version'] and destination.is_file() and media.sha256_file(destination) == item["sha256"]:
                return {"skipped": True, "reason": "already_published", "sha256": item["sha256"], 'publication_gate': gate}
            from . import publication
            publication.publish_intent(self.settings, job, item, current, gate, audit)
            result = media.publish(path, destination, item["sha256"], self.settings.roots, self.settings.publication_root,
                                   expected_destination_sha256=item['publication_target']['expected_ready_sha256'], backup_dir=backup_dir, authorize=authorize)
            result["audit"] = audit
            result['publication_gate'] = gate
        else:
            raise media.MediaError("unsupported_job", "Unsupported job")
        self.persist_mutation(job, item, result)
        return {key: val for key, val in result.items() if key not in {"backup_path", "journal_path"}}

    async def analyze_item(self, job, item):
        fingerprint = job.get('kind','analyze')=='recognize'
        if not (self.settings.external_fingerprints if fingerprint else self.settings.external_metadata):
            raise media.MediaError('provider_disabled', 'Внешние метаданные отключены')
        row = await self.io(db.fetchone, self.settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (item['track_id'],))
        if not row or row['sha256'] != item['sha256']:
            raise media.MediaError('conflict', 'File revision changed')
        path = self.settings.work_root/item['relative_path']
        await self.io(media._expect, path, item['sha256'])
        meta = json.loads(row['metadata'])
        if 'duration' in meta and not float(meta['duration'])>0:
            probe=await self.io(media.probe_audio,path);meta['duration']=probe['duration'];meta['duration_source']='ffprobe_fallback'
        if self.providers is None: self.providers = Providers(self.settings,metrics=self.metrics)
        def authorized():
            if db.setting(self.settings.db_path,'recovery_required','false')=='true':raise media.MediaError('recovery_required','Восстановление требует сверки')
            if int(db.setting(self.settings.db_path,'auth_epoch','0'))!=job['auth_epoch'] or self.cancelled(job['id']):
                raise media.MediaError('authorization_revoked','Analysis authorization revoked')
        authorized()
        identified=None; recognition_source=None
        if fingerprint:
            def current_audit(value):
                return (isinstance(value,dict) and value.get('state')=='passed'
                        and value.get('sha256')==item['sha256']
                        and value.get('profile')=='full_decode_all_audio_streams'
                        and isinstance(value.get('audio_identity'),str)
                        and media._SHA.fullmatch(value['audio_identity']))
            audit=meta.get('audit')
            if not current_audit(audit):
                await self.io(self.item_action,job,item,'decode')
                audit=dict(await self.io(media.audit_audio,path,self.settings.roots))
                if audit.get('sha256')!=item['sha256']:
                    raise media.MediaError('conflict','File changed before recognition audit')
                audit['checked_at']=db.utcnow()
                if audit.get('state')=='passed' and not current_audit(audit):
                    audit.update(state='unsupported',error='audit_profile_unsupported')
                authorized()
                def save_audit():
                    with db.transaction(self.settings.db_path) as conn:
                        fresh=conn.execute('SELECT metadata,status FROM tracks WHERE id=? AND sha256=? AND is_current=1',
                                           (item['track_id'],item['sha256'])).fetchone()
                        if not fresh:raise media.MediaError('conflict','Input revision changed during recognition audit')
                        data=json.loads(fresh['metadata']);data['audit']=audit
                        if audit.get('hash_verified_at'):data['hash_verified_at']=audit['hash_verified_at']
                        conn.execute('UPDATE tracks SET metadata=?,status=?,updated_at=? WHERE id=? AND sha256=? AND is_current=1',
                            (db.encode(data),'error' if audit.get('state')=='failed' else fresh['status'],db.utcnow(),item['track_id'],item['sha256']))
                        return data
                meta=await self.io(save_audit)
            if not current_audit(audit):
                raise media.MediaError('audit_failed','Полная проверка всех аудиопотоков не пройдена; внешнее распознавание не отправлено')
            # Reuse only server audit evidence for this exact file revision.
            # The packet identity survives tag-only changes; no second packet
            # probe is needed after the full audit.
            audio_identity=audit['audio_identity']
            await self.io(self.item_action,job,item,'fingerprint_sample')
            audio, offset=await self.io(recognition.sample,path,self.settings.roots,float(meta.get('duration',0)))
            try:
                with provider_attempts.recognition_scope(audio_identity,job['id']):
                    await self.io(self.item_action,job,item,'fingerprint_lookup')
                    identified=await recognition.recognize(self.providers,audio,offset,authorized,input_revision=item['sha256'])
                    alternate=min(max(0,float(meta.get('duration',0))-12),float(meta.get('duration',0))*.65)
                    if not identified and abs(alternate-offset)>=5:
                        audio,offset=await self.io(recognition.sample,path,self.settings.roots,float(meta.get('duration',0)),alternate)
                        identified=await recognition.recognize(self.providers,audio,offset,authorized,input_revision=item['sha256'])
                recognition_source={'provider':'shazam','state':'fingerprint_match' if identified else 'no_match'}
            except ProviderError as exc:
                recognition_source={'provider':'shazam','state':exc.code,'message':str(exc)}
        else:
            previous=await self.io(db.fetchone,self.settings.db_path,'SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?',(item['track_id'],item['sha256']))
            if previous:
                identified=next((c for c in json.loads(previous['payload']).get('candidates',[]) if c.get('provider')=='shazam' and c.get('tags')),None)
        if self.settings.external_metadata:
            await self.io(self.item_action,job,item,'metadata_lookup')
            result = await self.providers.analyze(meta, item['relative_path'],identified,authorized)
        else:
            result={'input_sha256':item['sha256'],'query_source':'audio_fingerprint','candidates':[],'sources':[],
                    'automatic_apply':False,'state':'incomplete'}
        if recognition_source:
            result['sources'].insert(0,recognition_source)
            if identified:
                result['candidates'].insert(0,identified);result['state']='candidates'
        # Album artwork already next to the track is a local candidate, never an arbitrary path.
        if not meta.get('has_art'):
            local = await self.io(self.local_cover, path)
            if local:
                result['candidates'].append(local)
                result['state']='candidates'
        await self.io(media._expect, path, item['sha256'])
        if int(db.setting(self.settings.db_path, 'auth_epoch', '0')) != job['auth_epoch'] or self.cancelled(job['id']):
            raise media.MediaError('authorization_revoked','Analysis authorization revoked')
        with db.transaction(self.settings.db_path) as conn:
            current = conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1',(item['track_id'],)).fetchone()
            if not current or current['sha256'] != item['sha256']: raise media.MediaError('conflict','File revision changed')
            previous=conn.execute('SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?',(item['track_id'],item['sha256'])).fetchone()
            if previous:
                old=json.loads(previous['payload'])
                for key in ('asr','alignment'):
                    if key in old: result[key]=old[key]
                result['candidates'].extend(c for c in old.get('candidates',[]) if c.get('provider') in {'independent_asr','local_replaygain'})
                if not fingerprint:
                    result['candidates'].extend(c for c in old.get('candidates',[]) if c.get('provider')=='shazam')
                    result['sources'].extend(s for s in old.get('sources',[]) if s.get('provider')=='shazam')
            result=provider_policy.prepare_analysis(conn,result)
            conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',(item['track_id'],item['sha256'],db.encode(result),db.utcnow()))
            db.event(conn,'analysis_completed',{'track_id':item['track_id'],'state':result['state']})
        retry=[]
        for source in result['sources']:
            if source.get('state') in {'recognition_attempt_budget_exhausted','recognition_identity_required'}:
                raise media.MediaError(source['state'],'Бюджет распознавания этой аудиозаписи исчерпан или не подтверждена аудиоревизия; требуется ручное решение')
            if source['provider'] not in ({'shazam'} if fingerprint else {'musicbrainz','lrclib','coverartarchive'}):continue
            if source['state'] not in {'rate_limited','provider_cooldown','provider_unavailable','budget_exhausted'}:continue
            breaker=json.loads(db.setting(self.settings.db_path,'provider_breaker:'+source['provider'],'{}'))
            due=breaker.get('until',time.time()+60)
            if source['state']=='budget_exhausted':
                first=db.fetchone(self.settings.db_path,'SELECT MIN(at) AS at FROM provider_calls WHERE provider=?',(source['provider'],))
                due=(first['at'] if first and first['at'] else time.time())+86401
            retry.append(max(time.time()+10,due))
        return {'state':result['state'],'candidates':len(result['candidates']),'sources':result['sources'],**({'retry_at':max(retry)} if retry else {})}

    async def process_youtube(self, job, item):
        def authorized():
            if db.setting(self.settings.db_path,'recovery_required','false')=='true' or self.cancelled(job['id']) or int(db.setting(self.settings.db_path,'auth_epoch','0'))!=job['auth_epoch']:
                raise media.MediaError('authorization_revoked','Разрешение задания отозвано')
        authorized()
        row=await self.io(db.fetchone,self.settings.db_path,'SELECT metadata,sha256 FROM tracks WHERE id=? AND is_current=1',(item['track_id'],))
        if not row or row['sha256']!=item['sha256']:raise media.MediaError('conflict','Версия файла изменилась')
        meta=json.loads(row['metadata']);path=self.settings.work_root/item['relative_path']
        await self.io(media._expect,path,item['sha256'])
        data=youtube.merge(await self.io(youtube.discover,path,self.settings,meta),meta.get('youtube'))
        params=json.loads(job['payload']).get('params',{});retry_at=0
        if params.get('youtube_lookup'):
            if self.providers is None:self.providers=Providers(self.settings,metrics=self.metrics)
            analysis=await self.io(db.fetchone,self.settings.db_path,'SELECT payload FROM analyses WHERE track_id=? AND input_sha256=?',(item['track_id'],item['sha256']))
            try:
                data=await youtube.enrich(self.providers,data,meta,item['relative_path'],json.loads(analysis['payload']) if analysis else {},params.get('youtube_search',False),authorized)
                data['lookup_complete']=True;data.pop('lookup_error',None)
            except ProviderError as exc:
                data.update(lookup_complete=False,lookup_error=exc.code)
                if exc.code=='provider_unavailable':await self.io(self.providers.unavailable,'youtube')
                if exc.code in {'provider_unavailable','rate_limited','provider_cooldown','budget_exhausted'}:
                    breaker=json.loads(db.setting(self.settings.db_path,'provider_breaker:youtube','{}'));retry_at=breaker.get('until',time.time()+60)
                    if exc.code=='budget_exhausted':
                        first=db.fetchone(self.settings.db_path,"SELECT MIN(at) AS at FROM provider_calls WHERE provider='youtube'")
                        retry_at=(first['at'] if first and first['at'] else time.time())+86401
                    retry_at=max(time.time()+10,retry_at)
        await self.io(media._expect,path,item['sha256']);authorized()
        with db.transaction(self.settings.db_path) as conn:
            current=conn.execute('SELECT metadata,sha256 FROM tracks WHERE id=? AND is_current=1',(item['track_id'],)).fetchone()
            if not current or current['sha256']!=item['sha256']:raise media.MediaError('conflict','Версия файла изменилась')
            latest=json.loads(current['metadata']);owner=latest.get('youtube',{})
            # Preserve a link/unlink submitted while the external request was running.
            if owner!=meta.get('youtube',{}) and owner.get('state') in {'owner_confirmed','unlinked'}:
                enriched=data;data=youtube.merge(data,owner)
                for key in ('lookup_complete','lookup_error','query_source','search_state'):
                    if key in enriched:data[key]=enriched[key]
            latest['youtube']=data
            conn.execute('UPDATE tracks SET metadata=?,updated_at=? WHERE id=?',(db.encode(latest),db.utcnow(),item['track_id']))
            db.event(conn,'youtube_sources_updated',{'track_id':item['track_id'],'state':data['state'],'candidates':len(data['candidates'])})
        return {'state':data['state'],'linked_id':data.get('linked_id'),'candidates':len(data['candidates']),**({'retry_at':retry_at} if retry_at else {})}

    def local_cover(self, path):
        for name in ('cover.jpg','folder.jpg','front.jpg','cover.png','folder.png'):
            candidates = [p for p in path.parent.iterdir() if p.name.casefold()==name and not p.is_symlink() and p.is_file()]
            if len(candidates)!=1: continue
            image=media._validate(candidates[0],self.settings.roots)
            if image.stat().st_size>5*1024**2: continue
            raw=image.read_bytes()
            try: mime,_,_=media._cover(raw)
            except media.MediaError: continue
            return {'id':'local:'+media.sha256_file(image),'provider':'local','kind':'cover','cover':'data:'+mime+';base64,'+base64.b64encode(raw).decode(),'source_path':str(image.relative_to(self.settings.work_root)),'match':{'state':'needs_review','reasons':['confirm_album_cover']}}
        return None

    async def process(self, job):
        self.state = job["kind"]
        if job["kind"] == "scan":
            return await self.scan(job)
        if job['kind'] == 'release_audit':
            from . import release_review
            return await release_review.process(self, job)
        if job['kind'] == 'publish_sidecars':
            from . import publication_sidecars
            return await publication_sidecars.process(self, job)
        if job['kind']=='replaygain_album':
            from . import album_loudness, album_gain_policy
            items=json.loads(job['payload'])['items']
            def authorized():
                if self.cancelled(job['id']) or int(db.setting(self.settings.db_path,'auth_epoch','0'))!=job['auth_epoch']:
                    raise media.MediaError('authorization_revoked','Album analysis authorization revoked')
            try:
                values=await self.io(album_loudness.analyze,[{**i,'path':self.settings.work_root/i['relative_path']} for i in items],self.settings.roots,authorized)
                album_context=await self.io(album_gain_policy.build,self.settings,items,json.loads(job['payload']).get('params',{}),authorized)
                with db.transaction(self.settings.db_path) as conn:
                    album_gain_policy.register(conn,album_context)
                    for item,candidate in zip(items,values):
                        candidate['album_gain_group']={'id':album_context['id'],'context_sha256':album_context['context_sha256']}
                        current=conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1',(item['track_id'],)).fetchone()
                        if not current or current['sha256']!=item['sha256']:raise media.MediaError('conflict','Album input changed')
                        previous=conn.execute('SELECT * FROM analyses WHERE track_id=?',(item['track_id'],)).fetchone()
                        data=json.loads(previous['payload']) if previous and previous['input_sha256']==item['sha256'] else {'candidates':[],'sources':[],'input_sha256':item['sha256'],'query_source':'local_audio'}
                        data['candidates']=[c for c in data['candidates'] if c.get('provider')!='local_replaygain']+[candidate]
                        data['sources']=[s for s in data['sources'] if s.get('provider')!='local_replaygain']+[{'provider':'local_replaygain','state':'candidate'}]
                        data['state']='candidates'
                        conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',(item['track_id'],item['sha256'],db.encode(data),db.utcnow()))
                    db.event(conn,'album_loudness_measured',{'job_id':job['id'],'files':len(items)})
                for item,candidate in zip(items,values):self.item_result(job['id'],item['track_id'],'succeeded',{'state':'needs_review','tags':candidate['tags']})
                self.finish(job['id'],'succeeded')
            except Exception as exc:
                for item in items:self.item_result(job['id'],item['track_id'],'failed',self.failure(exc))
                self.finish(job['id'],'failed',self.failure(exc)['code'])
            return
        if job['kind']=='export_ready':
            def authorized():
                if self.cancelled(job['id']) or int(db.setting(self.settings.db_path,'auth_epoch','0'))!=job['auth_epoch']:
                    raise media.MediaError('authorization_revoked','Export authorization revoked')
            with db.transaction(self.settings.db_path) as conn:conn.execute('UPDATE jobs SET total=1 WHERE id=?',(job['id'],))
            try:
                result=await self.io(exports.generate,self.settings,authorized)
                self.item_result(job['id'],'export','succeeded',result);self.finish(job['id'],'cancelled_partial' if self.cancelled(job['id']) else 'succeeded')
            except Exception as exc:
                failure=self.failure(exc)
                if failure['code']=='insufficient_space' and not self.cancelled(job['id']):
                    retry_at=time.time()+60;payload=json.loads(job['payload']);payload.update(retry_at=retry_at,wait_reason='storage')
                    self.item_result(job['id'],'export','deferred',{**failure,'retry_at':retry_at})
                    with db.transaction(self.settings.db_path) as conn:
                        conn.execute("UPDATE jobs SET state='queued',stage='waiting_storage',payload=?,updated_at=? WHERE id=?",(db.encode(payload),db.utcnow(),job['id']))
                        db.event(conn,'job_waiting',{'job_id':job['id'],'retry_at':retry_at,'reason':'storage'})
                else:
                    self.item_result(job['id'],'export','failed',failure);self.finish(job['id'],'cancelled' if self.cancelled(job['id']) else 'failed',failure['code'])
            return
        payload=json.loads(job['payload']);items=payload['items'];retry_at=0;waiting_storage=False
        done={r['item_key'] for r in db.fetchall(self.settings.db_path,"SELECT item_key FROM job_items WHERE job_id=? AND state IN ('succeeded','failed','conflict','needs_review')",(job['id'],))}
        if job['kind']=='publish' and payload.get('params',{}).get('publication_plan_id'):
            from . import api_facades
            try:
                with db.connect(self.settings.db_path) as conn:
                    plan=api_facades.load(conn,payload['params']['publication_plan_id'])
                await self.io(api_facades.validate_plan,self.settings,plan,skip=done)
            except Exception as exc:
                result=self.failure(exc)
                for item in items:
                    if item['track_id'] not in done:self.item_result(job['id'],item['track_id'],'conflict',result)
                self.finish(job['id'],'conflict',result['code'])
                return
        items=[i for i in items if i['track_id'] not in done]
        halted=False
        def stop_admission():
            nonlocal halted
            if not halted:
                if retry_at:
                    halted=True
                else:
                    control=self.job_control(job['id'])
                    halted=self.control_cancelled(control) or bool(control and control['pause_requested'])
            return halted
        async def one_inner(item):
            nonlocal retry_at,waiting_storage
            if stop_admission():
                return
            key = item["track_id"]
            self.item_result(job["id"], key, "running", {})
            try:
                if job['kind'] in {'transcribe','align'}:
                    result = await inference_client.process(self,job,item)
                elif job['kind']=='youtube':result=await self.process_youtube(job,item)
                else:
                    result = await self.analyze_item(job, item) if job['kind'] in {'analyze','recognize'} else await self.io(self.process_item, job, item)
                if result.get('retry_at'):
                    retry_at=max(retry_at,result['retry_at'])
                    self.item_result(job['id'],key,'deferred',result)
                else:self.item_result(job["id"], key, "succeeded", result)
            except Exception as exc:
                result = self.failure(exc)
                journals = self.settings.state_dir / "backups" / key / job["id"]
                has_intent = job["kind"] in {"apply", "rollback", "publish", "rollback_ready"} and journals.exists() and any(journals.glob("*.json"))
                if result['code']=='insufficient_space' and not has_intent:
                    # Only a failed reservation before any journal/effect may
                    # resume automatically. ENOSPC after intent stays recovery.
                    waiting_storage=True;retry_at=max(retry_at,time.time()+60)
                    self.item_result(job['id'],key,'deferred',{**result,'retry_at':retry_at,'wait_reason':'storage'})
                    return
                state = "interrupted" if has_intent else "conflict" if result["code"] in {"conflict", "revision_conflict"} else "needs_review" if result["code"] in {"needs_review", "invalid_lyrics", "unsupported_format", "authorization_revoked", "publication_snapshot_required", "publication_review_stale", "lyrics_document_stale", "lyrics_line_locked", "lyrics_document_conflict", "partial_lyrics", "recognition_attempt_budget_exhausted", "recognition_identity_required"} else "failed"
                if has_intent:
                    with db.transaction(self.settings.db_path) as conn:
                        conn.execute("UPDATE tracks SET status='needs_review' WHERE id=?", (key,))
                    result["recovery_required"] = True
                self.item_result(job["id"], key, state, result)
        async def one(item):
            with context(track_id=opaque_id(item['track_id'])): await one_inner(item)
        await self.bounded_items(items, one, should_stop=stop_admission)
        current=db.fetchone(self.settings.db_path,'SELECT * FROM jobs WHERE id=?',(job['id'],))
        paused=json.loads(current['payload']).get('pause_requested')
        if paused and current['completed']+current['failed']<current['total'] and not self.cancelled(job['id']):
            with db.transaction(self.settings.db_path) as conn:
                conn.execute("UPDATE jobs SET state='paused',stage='paused',updated_at=? WHERE id=?",(db.utcnow(),job['id']))
                db.event(conn,'job_paused',{'job_id':job['id']})
            log('job_paused',job_id=job['id'],kind=job['kind'])
            return
        if retry_at and not self.cancelled(job['id']):
            payload=json.loads(current['payload']);payload['retry_at']=retry_at;payload['wait_reason']='storage' if waiting_storage else 'provider'
            stage='waiting_storage' if waiting_storage else 'waiting_provider'
            with db.transaction(self.settings.db_path) as conn:
                conn.execute("UPDATE jobs SET state='queued',stage=?,payload=?,updated_at=? WHERE id=?",(stage,db.encode(payload),db.utcnow(),job['id']))
                db.event(conn,'job_waiting',{'job_id':job['id'],'retry_at':retry_at,'reason':payload['wait_reason']})
            log('job_'+stage,logging.WARNING,job_id=job['id'],kind=job['kind'],retry_at=retry_at)
            return
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
            if stats['completed']:
                state='completed_with_errors'
            else:
                failures={row['state'] for row in db.fetchall(self.settings.db_path,"SELECT DISTINCT state FROM job_items WHERE job_id=? AND state IN ('failed','conflict','needs_review')",(job['id'],))}
                state=next(iter(failures)) if len(failures)==1 else 'failed'
        else:
            state = "succeeded"
        self.finish(job["id"], state)

    def recover_jobs(self):
        """Mark interrupted before inspecting journals; never replay a mutation."""
        rows = db.fetchall(self.settings.db_path, "SELECT * FROM jobs WHERE state IN ('running','interrupted')")
        for job in rows:
            self.finish(job["id"], "interrupted", "worker_restarted")
            if job['kind'] == 'publish_sidecars' and self.settings.writable:
                # The owner grants a fresh retry before any uncommitted effect.
                # That retry reconciles existing per-object journals first.
                from . import publication_sidecars
                plan_id = json.loads(job['payload'])['params']['plan_id']
                plan = publication_sidecars.load_plan(self.settings, plan_id)
                if plan['state'] == 'complete':
                    self.finish(job['id'], 'succeeded', 'sidecar_plan_already_committed')
                else:
                    publication_sidecars.update_plan(self.settings, plan_id, state='partial' if plan['results'] else 'interrupted', error='worker_restarted')
                    self.finish(job['id'], 'needs_review', 'sidecar_recovery_required')
                continue
            if not self.settings.writable or job["kind"] not in {"apply", "publish", "rollback", "rollback_ready"}:
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
                            if 'metadata_slot_targets' in restored: result['metadata_slot_targets'] = restored['metadata_slot_targets']
                            self.persist_mutation(job, item, result)
                            self.item_result(job["id"], item["track_id"], "succeeded", {"recovered": True, "sha256": result["sha256"]})
                    except Exception as exc:
                        self.item_result(job["id"], item["track_id"], "conflict", self.failure(exc))
            totals = db.fetchone(self.settings.db_path, "SELECT total,completed FROM jobs WHERE id=?", (job["id"],))
            if totals["total"] and totals["total"] == totals["completed"]:
                self.finish(job["id"], "succeeded", "recovered_after_restart")

    async def background_job(self, job):
        self.metrics.jobs_active.labels(job['kind']).inc()
        token = context(job_id=job['id'],kind=job['kind']); token.__enter__()
        log('job_started',job_id=job['id'],kind=job['kind'],total=job['total'])
        try:
            await self.process(job)
        except Exception as exc:
            state = 'interrupted' if job['kind'] in {'apply','rollback','publish','rollback_ready','publish_sidecars'} else 'failed'
            self.finish(job['id'],state,self.failure(exc)['code'])
            if state=='interrupted': self.stopping=True
        finally:
            self.state='idle'
            self.metrics.jobs_active.labels(job['kind']).dec()
            token.__exit__(None,None,None)

    async def run(self, once=False):
        self.settings.validate()
        db.initialize(self.settings.db_path, settings=self.settings)
        with ExitStack() as stack:
            stack.enter_context(scheduler_guard(self.settings.state_dir))
            verify_storage(self.settings)
            if self.settings.writable:
                stack.enter_context(media.writer_guard(self.settings.storage_guard_dir, self.settings.instance_id))
            await self.io(self.recover_jobs)
            self.state = "idle"
            await asyncio.to_thread(self.heartbeat)
            heartbeat_task = asyncio.create_task(self.heartbeats())
            next_schedule=0
            try:
                while not self.stopping:
                    if heartbeat_task.done():
                        # A schema/I/O error must remain visible, rather than
                        # silently losing the heartbeat task permanently.
                        heartbeat_task.result()
                    try:
                        if time.monotonic()>=next_schedule:
                            await self.io(provider_policy.cleanup_cache,self.settings)
                            from . import provider_attempts
                            await self.io(provider_attempts.recover_stale,self.settings)
                            await self.io(scheduling.tick,self.settings)
                            next_schedule=time.monotonic()+30
                        # SQLite's busy_timeout can wait 15s. Keep this wait off
                        # both the event loop and the bounded media I/O pool.
                        job = await asyncio.to_thread(self.claim_job)
                    except sqlite3.OperationalError as exc:
                        if not sqlite_busy(exc):
                            raise
                        # The transaction context rolled back and closed its
                        # connection. Retry a fresh poll after a bounded await;
                        # never acknowledge or restart a partially claimed job.
                        log('worker_database_busy', logging.WARNING, stage='scheduler')
                        await asyncio.sleep(DB_BUSY_RETRY_SECONDS)
                        continue
                    if job:
                        task=asyncio.create_task(self.background_job(job))
                        if job['kind'] in {'transcribe','align'}: self.inference_task=task
                        else: self.normal_task=task
                        continue
                    pending=[task for task in (self.normal_task,self.inference_task) if task and not task.done()]
                    if once and not pending: break
                    # Poll claims while either lane runs, so a newly queued ASR
                    # can start during a long metadata batch (and vice versa).
                    if pending:
                        await asyncio.wait(pending,timeout=.1 if once else .5,return_when=asyncio.FIRST_COMPLETED)
                    else: await asyncio.sleep(.5)
            finally:
                self.stopping = True
                for task in (self.normal_task,self.inference_task):
                    if task and not task.done():
                        try: await asyncio.wait_for(task,timeout=20)
                        except (asyncio.TimeoutError,asyncio.CancelledError): pass
                heartbeat_task.cancel()
                self.state = "stopped"
                # This must precede every fallible database/logging operation.
                # Cancelled asyncio futures can still have live executor writes;
                # drain synchronously while both scheduler/storage guards remain
                # held, with no await/cancellation point before completion.
                self.pool.shutdown(wait=True, cancel_futures=True)
                if self.providers is not None:
                    await self.providers.close()
                try:
                    self.heartbeat()
                except Exception:
                    LOG.warning('Could not persist the final worker heartbeat',exc_info=True,extra={'event':'heartbeat_persist_failed'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Drain queued jobs, then stop")
    parser.add_argument("--health", action="store_true", help="Check worker heartbeat")
    args = parser.parse_args()
    configure_logging('worker')
    settings = Settings.from_env()
    if args.health:
        heartbeat = json.loads(db.setting(settings.db_path, "worker_heartbeat", "{}"))
        return 0 if time.time()-heartbeat.get("at", 0) < 30 and heartbeat.get("state") != "stopped" else 1
    worker = Worker(settings)
    server = worker_server(worker.metrics,settings.metrics_token_file,9000) if settings.metrics_token_file else None
    log('worker_starting',count=settings.file_workers)
    def stop(*_):
        worker.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try: asyncio.run(worker.run(args.once))
    finally:
        if server: server.shutdown(); server.server_close()
        log('worker_stopped')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
