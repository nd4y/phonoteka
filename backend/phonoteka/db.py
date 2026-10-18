"""Short SQLite transactions; each call owns its connection and never a media handle."""

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import uuid

SCHEMA_VERSION = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=15, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA synchronous=FULL")
    return db


@contextmanager
def transaction(path: Path):
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, SCHEMA_VERSION}:
            raise RuntimeError("Unsupported database schema; compatible image required")
        db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, created REAL NOT NULL,
            expires REAL NOT NULL, auth_epoch INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY, client TEXT NOT NULL, at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS login_attempts_client ON login_attempts(client, at);
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 1,
            sha256 TEXT NOT NULL, metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'indexed',
            scan_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS tracks_current_path ON tracks(relative_path) WHERE is_current=1;
        CREATE INDEX IF NOT EXISTS tracks_scan ON tracks(scan_id);
        CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY, track_id TEXT NOT NULL REFERENCES tracks(id), input_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS proposals_track ON proposals(track_id,created_at);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL,
            payload TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
            auth_epoch INTEGER NOT NULL DEFAULT 0, heartbeat REAL
        );
        CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,created_at);
        CREATE TABLE IF NOT EXISTS job_items (
            job_id TEXT NOT NULL REFERENCES jobs(id), item_key TEXT NOT NULL,
            state TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(job_id,item_key)
        );
        CREATE TABLE IF NOT EXISTS idempotency (
            scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
            job_id TEXT NOT NULL REFERENCES jobs(id), created_at TEXT NOT NULL,
            PRIMARY KEY(scope,key)
        );
        CREATE TABLE IF NOT EXISTS history (
            id TEXT PRIMARY KEY, track_id TEXT NOT NULL REFERENCES tracks(id), job_id TEXT,
            kind TEXT NOT NULL, before_sha256 TEXT, after_sha256 TEXT,
            details TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS history_track ON history(track_id,created_at);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analyses (
            track_id TEXT PRIMARY KEY REFERENCES tracks(id), input_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_cache (
            key TEXT PRIMARY KEY, provider TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_calls (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS provider_calls_time ON provider_calls(provider,at);
        PRAGMA user_version=1;
        COMMIT;
        """)
        db.execute("INSERT OR IGNORE INTO settings VALUES('auth_epoch','1')")
        db.execute("INSERT OR IGNORE INTO settings VALUES('instance_uuid',?)", (str(uuid.uuid4()),))
        db.commit()


def fetchone(path: Path, sql: str, params=()):
    with connect(path) as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def fetchall(path: Path, sql: str, params=()):
    with connect(path) as db:
        return [dict(row) for row in db.execute(sql, params).fetchall()]


def setting(path: Path, key: str, default=None):
    row = fetchone(path, "SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(path: Path, key: str, value: str):
    with transaction(path) as db:
        db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,value))


def event(db: sqlite3.Connection, kind: str, payload: dict):
    db.execute("INSERT INTO events(kind,payload,created_at) VALUES(?,?,?)", (kind,encode(payload),utcnow()))
    # Both time and count cap replay; clients outside retained IDs explicitly resync.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="milliseconds")
    db.execute("DELETE FROM events WHERE id <= COALESCE((SELECT MAX(id)-10000 FROM events),0) OR created_at < ?", (cutoff,))
