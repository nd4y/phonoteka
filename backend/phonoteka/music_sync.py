"""Deliver confirmed Phonoteka audio to an existing, separately managed Music tree."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time
from uuid import uuid4

import httpx

from phonoteka import catalog_layout, media, provider_policy, providers, publication_paths


STAGING = ".phonoteka-sync"
SETTLED = {"succeeded", "completed_with_errors", "cancelled_partial"}
TERMINAL = SETTLED | {"cancelled", "failed", "conflict"}
HEX = re.compile(r"[0-9a-f]{64}")
ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
ROOT_IDENTITY = "statvfs-fsid-inode-v1"


class Deferred(Exception):
    """The published record is unavailable or the destination needs a decision."""


@dataclass(frozen=True)
class Approved:
    track_id: str
    sha256: str
    operation_id: str
    job_id: str
    path: str


def read_json(path: Path) -> dict:
    with media._file(path) as stream:
        return json.load(stream)


def validate_root_identity(value) -> None:
    # Legacy ledgers remain supported, but are never silently rebound.
    if (isinstance(value, list) and len(value) == 2
            and all(type(item) is int and item >= 0 for item in value)):
        return
    if (isinstance(value, dict) and set(value) == {"kind", "fsid", "inode"}
            and value["kind"] == ROOT_IDENTITY
            and isinstance(value["fsid"], str) and re.fullmatch(r"[0-9a-f]{16}", value["fsid"])
            and int(value["fsid"], 16) != 0
            and isinstance(value["inode"], str) and re.fullmatch(r"[1-9][0-9]*", value["inode"])):
        return
    raise Deferred("invalid_music_identity")


def music_root_identity(path: Path) -> dict[str, str]:
    """Identify the filesystem and directory without an ephemeral device number."""
    with media._directory(path) as fd:
        if fd < 0 or not hasattr(os, "fstatvfs"):
            raise Deferred("music_identity_unavailable")
        info = os.fstat(fd)
        fsid = getattr(os.fstatvfs(fd), "f_fsid", None)
        if type(fsid) is not int or not 0 < fsid < 2**64 or info.st_ino <= 0:
            raise Deferred("music_identity_unavailable")
        return {"kind": ROOT_IDENTITY, "fsid": f"{fsid:016x}", "inode": str(info.st_ino)}


def relative_path(value: str) -> Path:
    publication_paths.key(value)
    path = Path(value)
    if path.parts[0] == STAGING:
        raise Deferred("reserved_destination")
    return path


def stamp(path: Path) -> list[int] | None:
    media._no_links(path, missing=True)
    try:
        s = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(s.st_mode):
        raise Deferred("not_a_regular_file")
    return file_stamp(s)


def file_stamp(s: os.stat_result) -> list[int]:
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


@contextmanager
def music_file(path: Path):
    """Read a Music inode without following links or editing any of its aliases."""
    with media._directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Deferred("not_a_regular_file")
            yield stream


def stable_hash(path: Path) -> tuple[str | None, list[int] | None]:
    before = stamp(path)
    if before is None:
        return None, None
    with music_file(path) as stream:
        if file_stamp(os.fstat(stream.fileno())) != before:
            raise Deferred("file_changed_during_read")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if stamp(path) != before:
        raise Deferred("file_changed_during_read")
    return digest, before


def preserve_music_access(source: Path, staged: Path, expected: list[int]) -> None:
    # A Drive index may hold another link to the old inode. Copy its access
    # metadata to our private inode and leave every alias of the old one intact.
    with music_file(source) as before, media._file(staged) as after:
        info = os.fstat(before.fileno())
        if file_stamp(info) != expected:
            raise Deferred("music_changed_during_copy")
        dst = after.fileno()
        current = os.fstat(dst)
        if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
            os.fchown(dst, info.st_uid, info.st_gid)
        os.fchmod(dst, stat.S_IMODE(info.st_mode))
        attrs = {key: os.getxattr(before.fileno(), key) for key in os.listxattr(before.fileno())}
        for key in os.listxattr(dst):
            if key not in attrs:
                os.removexattr(dst, key)
        for key, value in attrs.items():
            os.setxattr(dst, key, value)
        if any(os.getxattr(dst, key) != value for key, value in attrs.items()):
            raise Deferred("permissions_not_preserved")
        os.fsync(dst)


def sync_music_file(path: Path, inode: list[int]) -> None:
    with music_file(path) as stream:
        if file_stamp(os.fstat(stream.fileno()))[:2] != inode:
            raise Deferred("music_changed_during_commit")
        os.fsync(stream.fileno())


def exchange(source: Path, target: Path) -> None:
    """Swap names while keeping the displaced inode, including open SMB handles."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise Deferred("atomic_exchange_unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    with media._directory(source.parent) as source_dir, media._directory(target.parent) as target_dir:
        if rename(source_dir, os.fsencode(source.name), target_dir, os.fsencode(target.name), 2):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))


class Catalogue:
    def __init__(self, client):
        self.client = client
        self.jobs = {}

    def get(self, path, **params):
        # Native authenticated read surface. Never request a proposal or an ASR draft.
        with self.client.stream("GET", path, params=params) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 12 * 1024 * 1024:
                    raise Deferred("catalogue_response_too_large")
        return json.loads(body)

    def job(self, job_id, *, fresh=False):
        if not ID.fullmatch(job_id):
            raise Deferred("invalid_job_id")
        if fresh or job_id not in self.jobs:
            self.jobs[job_id] = self.get("/api/jobs/" + job_id)
        return self.jobs[job_id]

    def ingest(self, state):
        """Persist a queue from settled jobs before touching any destination."""
        seen = set(state["seen_jobs"])
        incoming = []
        for kind in ("publish", "rollback_ready"):
            page = 1
            while True:
                result = self.get("/api/jobs", kind=kind, page=page, page_size=50)
                items = result["items"]
                for item in items:
                    if item["id"] not in seen and item["state"] in TERMINAL:
                        incoming.append(item)
                if not items or page * 50 >= result["total"]:
                    break
                page += 1
        if not incoming:
            return False
        for item in sorted(incoming, key=lambda x: (x["updated_at"], x["id"])):
            if item["state"] in SETTLED:
                job = self.job(item["id"])
                if job["state"] not in SETTLED:
                    continue
                for result in job["results"]:
                    if result["item_state"] == "succeeded":
                        track_id = result["track_id"]
                        if not ID.fullmatch(track_id):
                            raise Deferred("invalid_track_id")
                        state["queue"][track_id] = job["id"]
            seen.add(item["id"])
        state["seen_jobs"] = sorted(seen)
        return True

    def approved(self, track_id: str) -> Approved:
        if not ID.fullmatch(track_id):
            raise Deferred("invalid_track_id")
        plan = self.get("/api/tracks/" + track_id + "/publication")
        publication = plan.get("publication") or {}
        revision = publication.get("sha256") or ""
        if not plan["current_track"] or not HEX.fullmatch(revision):
            raise Deferred("no_confirmed_audio")
        job = self.job(publication.get("job_id") or "", fresh=True)
        operation = publication.get("operation_id")
        item = next((x for x in job["results"] if x["track_id"] == track_id and x["item_state"] == "succeeded"), None)
        history = any(x["history_id"] == operation and x["ready_revision"] == revision and x["kind"] == job["kind"]
                      for x in plan["versions"])
        if (job["state"] not in SETTLED or not item or not history
                or item.get("operation_id") != operation or item.get("sha256") != revision):
            raise Deferred("publication_not_committed")
        if job["kind"] == "publish":
            if plan["work_revision"] != revision or plan["gate"]["state"] != "passed":
                raise Deferred("publication_review_required")
        elif job["kind"] == "rollback_ready":
            if (not publication.get("restored_from_history") or publication.get("lyrics_gate", {}).get("state") != "passed"
                    or plan["gate"]["state"] == "blocked"):
                raise Deferred("rollback_not_confirmed")
            # The native read plan cannot evaluate historical provider rights.
            if provider_policy.media_sources(publication.get("usage_provenance", {})):
                raise Deferred("rollback_provider_rights_require_review")
        else:
            raise Deferred("publication_not_committed")
        relative_path(plan["ready_path"])
        return Approved(track_id, revision, operation, job["id"], plan["ready_path"])


class Delivery:
    def __init__(self, config: dict, state_dir: Path, client):
        self.music = Path(config["music"])
        self.ready = Path(config["ready"])
        self.state_dir = state_dir
        self.stage = self.music / STAGING
        self.identity = config["music_identity"]
        validate_root_identity(self.identity)
        self.catalogue = Catalogue(client)
        self.state_path = state_dir / "state.json"
        self.state = read_json(self.state_path)
        if self.state.get("schema") != 1 or self.state.get("music_identity") != self.identity:
            raise Deferred("wrong_delivery_ledger")
        self.index = publication_paths.Index()
        for track_id, record in self.state["tracks"].items():
            if not ID.fullmatch(track_id) or not HEX.fullmatch(record["sha256"]):
                raise Deferred("invalid_delivery_ledger")
            relative_path(record["path"])
            publication_paths.add(self.index, record["path"])

    def check_root(self):
        for path in (self.music, self.ready, self.state_dir):
            media._no_links(path)
            if not path.is_dir():
                raise Deferred("mount_missing")
        if isinstance(self.identity, list):
            with media._directory(self.music) as fd:
                s = os.fstat(fd)
            current = [s.st_dev, s.st_ino]
        else:
            current = music_root_identity(self.music)
        if current != self.identity:
            raise Deferred("music_root_changed")

    def save(self):
        media._journal(self.state_path, self.state)

    def destination(self, approved: Approved) -> str:
        old = self.state["tracks"].get(approved.track_id)
        if old:
            # A stable path keeps all existing playlists and Drive selections valid.
            if Path(old["path"]).suffix.lower() != Path(approved.path).suffix.lower():
                raise Deferred("format_change_requires_path_review")
            return old["path"]
        metadata = media.inspect_audio(self.ready / relative_path(approved.path), {"source": self.ready})
        tags = {key: providers.scalar(metadata["tags"], key)
                for key in ("title", "artist", "album", "albumartist", "discnumber", "tracknumber")}
        entry = {"track_id": approved.track_id, "path": approved.path, **tags}
        relative = catalog_layout.audio_paths([entry])[approved.track_id]
        # Do not allocate a path colliding by Unicode/case with an existing record.
        copy = publication_paths.Index()
        copy.update(self.index)
        copy.directories.update(self.index.directories)
        publication_paths.add(copy, relative)
        return relative

    def target(self, relative: str) -> Path:
        path = self.music / relative_path(relative)
        media._no_links(path, missing=True)
        # The SMB/Windows view is case-insensitive even though the NAS is not.
        parent = self.music
        for name in path.relative_to(self.music).parts:
            if not parent.exists():
                break
            folded = publication_paths.key(name)
            for sibling in parent.iterdir():
                if sibling.name != name and _same_name(sibling.name, folded):
                    raise Deferred("music_path_case_conflict")
            parent = parent / name
        return path

    def finish(self, pending):
        track_id = pending["track_id"]
        record = {"path": pending["path"], "sha256": pending["after"], "publication_id": pending["publication_id"],
                  "job_id": pending["job_id"], "delivered_at": time.time(),
                  "backup": pending["staged"] if pending["before"] else None}
        receipts = self.state_dir / "receipts"
        media._make_directory(receipts)
        media._journal(receipts / (Path(pending["staged"]).stem + ".json"), {
            "track_id": track_id, "before": pending["before"], **record,
        })
        if track_id not in self.state["tracks"]:
            publication_paths.add(self.index, pending["path"])
        self.state["tracks"][track_id] = record
        self.state["queue"].pop(track_id, None)
        self.state["pending"] = None
        self.state["conflicts"].pop(track_id, None)
        self.state["delivered_count"] += 1
        self.save()
        print(json.dumps({"event": "music_delivered", "track_id": track_id, "sha256": pending["after"],
                          "publication_id": pending["publication_id"]}), flush=True)

    def hold(self, track_id, revision, code, target=None, pending=None):
        self.state["conflicts"][track_id] = {"revision": revision, "code": code,
                                              "stamp": stamp(target) if target else None,
                                              "backup": pending["staged"] if pending else None}
        self.state["pending"] = None
        self.save()

    def reconcile_pending(self):
        pending = self.state["pending"]
        if not pending:
            return
        target = self.target(pending["path"])
        staged = self.stage / pending["staged"]
        if staged.parent != self.stage or not re.fullmatch(r"[0-9a-f]{32}\.tmp", staged.name):
            raise Deferred("invalid_pending_stage")
        actual, target_stamp = stable_hash(target)
        displaced, staged_stamp = stable_hash(staged)
        after_inode = pending.get("after_inode")
        before_inode = pending.get("before_inode")
        if (pending["phase"] == "ready" and actual == pending["after"] and displaced == pending["before"]
                and target_stamp[:2] == after_inode
                and (staged_stamp[:2] if staged_stamp else None) == before_inode):
            sync_music_file(target, after_inode)
            media._sync_directory(target.parent)
            media._sync_directory(self.stage)
            self.finish(pending)
        elif actual == pending["before"] and (
            pending["phase"] == "staging" or displaced == pending["after"]
        ):
            # The write has not happened. A later cycle can evaluate fresh approval.
            if staged_stamp is not None:
                with media._directory(self.stage) as directory:
                    os.unlink(staged.name, dir_fd=directory)
                    os.fsync(directory)
            self.state["pending"] = None
            self.save()
        else:
            self.hold(pending["track_id"], pending["after"], "interrupted_or_concurrent_write", target, pending)

    def deliver(self, approved: Approved):
        self.check_root()
        relative = self.destination(approved)
        target = self.target(relative)
        old = self.state["tracks"].get(approved.track_id, {})
        before = old.get("sha256")
        saved = self.state["conflicts"].get(approved.track_id)
        if saved and saved["revision"] == approved.sha256 and saved["stamp"] == stamp(target):
            return
        actual, target_stamp = stable_hash(target)
        if actual != before:
            self.hold(approved.track_id, approved.sha256, "music_revision_conflict", target)
            return
        source = self.ready / relative_path(approved.path)
        source_stamp = stamp(source)
        if not source_stamp:
            raise Deferred("ready_file_missing")
        if os.statvfs(self.music).f_bavail * os.statvfs(self.music).f_frsize < source_stamp[2] + 1024**3:
            raise Deferred("music_space_low")
        media._make_directory(self.stage)
        if self.stage.stat().st_dev != self.music.stat().st_dev:
            raise Deferred("staging_mount_changed")
        media._make_directory(target.parent)
        pending = {"track_id": approved.track_id, "path": relative, "before": before, "after": approved.sha256,
                   "staged": uuid4().hex + ".tmp", "publication_id": approved.operation_id, "job_id": approved.job_id,
                   "phase": "staging"}
        self.state["pending"] = pending
        self.save()
        staged = self.stage / pending["staged"]
        media._copy(source, staged)
        staged_hash, staged_stamp = stable_hash(staged)
        if staged_hash != approved.sha256 or stamp(source) != source_stamp:
            raise Deferred("ready_file_revision_conflict")
        if before:
            preserve_music_access(target, staged, target_stamp)
        else:
            with media._file(staged) as stream:
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
        self.check_root()
        if self.catalogue.approved(approved.track_id) != approved:
            raise Deferred("publication_changed_during_copy")
        if self.target(relative) != target or stamp(target) != target_stamp:
            self.hold(approved.track_id, approved.sha256, "music_changed_during_copy", target, pending)
            return
        pending["phase"] = "ready"
        pending["after_inode"] = staged_stamp[:2]
        pending["before_inode"] = target_stamp[:2] if target_stamp else None
        self.save()
        if before:
            exchange(staged, target)
        else:
            media._replace(staged, target, no_replace=True)
        actual_after, after_stamp = stable_hash(target)
        displaced, backup_stamp = stable_hash(staged)
        if (actual_after != approved.sha256 or displaced != before
                or (after_stamp[:2] if after_stamp else None) != pending["after_inode"]
                or (backup_stamp[:2] if backup_stamp else None) != pending["before_inode"]):
            # Restore the concurrent writer's displaced file when our new bytes
            # still occupy the target. Both inodes survive even a second race.
            if before and displaced is not None and actual_after == approved.sha256:
                exchange(staged, target)
                media._sync_directory(target.parent)
                media._sync_directory(self.stage)
            self.hold(approved.track_id, approved.sha256, "music_changed_during_commit", target, pending)
            return
        sync_music_file(target, pending["after_inode"])
        media._sync_directory(target.parent)
        media._sync_directory(self.stage)
        self.finish(pending)

    def cycle(self):
        self.check_root()
        self.reconcile_pending()
        if self.catalogue.ingest(self.state):
            self.save()
        changes = list(self.state["queue"])
        deferred = {}
        initial_count = self.state["delivered_count"]
        for track_id in changes:
            try:
                approved = self.catalogue.approved(track_id)
                if self.state["tracks"].get(track_id, {}).get("sha256") == approved.sha256:
                    self.state["queue"].pop(track_id)
                    self.save()
                    continue
                self.deliver(approved)
            except (Deferred, media.MediaError, OSError, httpx.HTTPError) as exc:
                deferred[track_id] = reason(exc)
                if self.state["pending"]:
                    self.reconcile_pending()
            if self.state["delivered_count"] - initial_count >= 8:
                break
        return {"observed_at": time.time(), "state": "watching", "registered": len(self.state["tracks"]),
                "delivered_count": self.state["delivered_count"], "eligible_changes": len(self.state["queue"]),
                "conflicts": self.state["conflicts"], "deferred": deferred}


def _same_name(name, expected):
    try:
        return publication_paths.key(name) == expected
    except media.MediaError:
        return False


def reason(exc):
    if isinstance(exc, media.MediaError):
        return exc.code
    if isinstance(exc, Deferred):
        return str(exc)
    if isinstance(exc, OSError):
        return f"os_error_{exc.errno}"
    return type(exc).__name__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/opt/music-sync/config.json"))
    parser.add_argument("--state", type=Path, default=Path("/sync-state"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    status_path = args.state / "status.json"
    if args.health:
        status = read_json(status_path)
        return 0 if status["state"] == "watching" and time.time() - status["observed_at"] < 600 else 1
    config = read_json(args.config)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    os.nice(12)
    media._no_links(args.state)
    lock_fd = os.open(args.state / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock, httpx.Client(
        base_url=config["api_url"], timeout=15, trust_env=False, follow_redirects=False,
    ) as client:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        last = None
        while not stop.is_set():
            try:
                status = Delivery(config, args.state, client).cycle()
            except (Deferred, OSError, httpx.HTTPError, media.MediaError, ValueError, KeyError) as exc:
                status = {"observed_at": time.time(), "state": "blocked", "reason": reason(exc)}
            media._journal(status_path, status)
            summary = {k: v for k, v in status.items() if k != "observed_at"}
            if summary != last:
                print(json.dumps({"event": "music_sync_status", **summary}), flush=True)
                last = summary
            if args.once:
                return 0 if status["state"] == "watching" else 1
            stop.wait(config.get("poll_seconds", 30))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
