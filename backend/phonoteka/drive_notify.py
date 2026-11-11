"""Notify Synology Drive on the NAS for exact, already delivered Music revisions."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


BASE = Path("/sync-state")
MUSIC = Path("/music")
LEDGER = BASE / "state.json"
STATE = BASE / "drive-notice-state"
SHA256 = re.compile(r"[0-9a-f]{64}")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class Changed(Exception):
    """A different file or publication revision occupies the path."""


def read(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def save(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary.exists():
                temporary.unlink()


def revision(record):
    return [record.get(key) for key in ("path", "sha256", "job_id", "publication_id")]


def identity(path, expected=None):
    if path.resolve() != path or not path.is_dir():
        raise ValueError("invalid_music_root")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if isinstance(expected, dict):
            fsid = getattr(os.fstatvfs(fd), "f_fsid", None)
            if type(fsid) is not int or not 0 < fsid < 2**64 or info.st_ino <= 0:
                raise ValueError("music_identity_unavailable")
            return {"kind": "statvfs-fsid-inode-v1", "fsid": format(fsid, "016x"), "inode": str(info.st_ino)}
        return [info.st_dev, info.st_ino]
    finally:
        os.close(fd)


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def notify(music, music_identity, ledger_path, track_id, record):
    relative, expected, job_id, operation = revision(record)
    if not SHA256.fullmatch(expected or "") or not UUID.fullmatch(job_id or "") or not operation:
        raise ValueError("invalid_delivery_receipt")
    if not relative or "\\" in relative or any(part in ("", ".", "..", ".phonoteka-sync") for part in relative.split("/")):
        raise ValueError("invalid_delivery_path")
    target = music / relative
    if target.resolve() != target:
        raise Changed()
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Changed() from None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or os.readlink(f"/proc/self/fd/{fd}") != str(target):
            raise Changed()
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        if digest.hexdigest() != expected:
            raise Changed()
        latest = read(ledger_path)
        if (latest["music_identity"] != music_identity or identity(music, music_identity) != music_identity
                or revision(latest["tracks"].get(track_id, {})) != revision(record)):
            raise Changed()
        if fingerprint(before) != fingerprint(os.fstat(fd)) or fingerprint(before) != fingerprint(target.lstat()):
            raise Changed()
        os.utime(fd, ns=(before.st_atime_ns, time.time_ns()))
        os.fsync(fd)
        after = os.fstat(fd)
        if fingerprint(after) != fingerprint(target.lstat()):
            raise Changed()
    return {"status": "notified", "mtime": int(after.st_mtime), "at": time.time(), "size": after.st_size}


def run(music, ledger_path, state_path):
    ledger = read(ledger_path)
    music_identity = identity(music, ledger.get("music_identity"))
    if ledger["schema"] != 1 or ledger["music_identity"] != music_identity:
        raise ValueError("wrong_music_ledger")
    ledger_notice = state_path / "notifications.json"
    notices = read(ledger_notice) if ledger_notice.is_file() else {}
    pending = [(key, value) for key, value in ledger["tracks"].items()
               if value.get("delivered_at") and revision(value) != notices.get(key, {}).get("revision")]
    updated = 0
    completed = 0
    errors = {}
    for track_id, record in pending:
        if time.time() - record["delivered_at"] < 15:
            continue
        try:
            outcome = notify(music, music_identity, ledger_path, track_id, record)
        except Changed:
            outcome = {"status": "conflict", "at": time.time()}
        except (OSError, ValueError, KeyError) as exc:
            errors[track_id] = "os_error_" + str(exc.errno) if isinstance(exc, OSError) else type(exc).__name__
            continue
        notices[track_id] = {"revision": revision(record), **outcome}
        save(ledger_notice, notices)
        updated += outcome["status"] == "notified"
        completed += 1
        if completed == 8:
            break
    status = {"observed_at": time.time(), "notified": updated,
              "pending": sum(revision(value) != notices.get(key, {}).get("revision") for key, value in pending),
              "conflicts": sum(item["status"] == "conflict" for item in notices.values()), "errors": errors}
    save(state_path / "status.json", status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--music", type=Path, default=MUSIC)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args(argv)
    if (os.getuid(), os.getgid()) != (1026, 100) or args.state.resolve() != args.state:
        raise SystemExit("wrong_scheduler_identity")
    os.nice(12)
    lock = os.open(args.state / "lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        status = run(args.music, args.ledger, args.state)
        if status["errors"]:
            raise SystemExit("drive_notification_deferred")
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
