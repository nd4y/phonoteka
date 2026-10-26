"""Short SQLite transactions; each call owns its connection and never a media handle."""

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from . import audit_status

SCHEMA_VERSION = 2


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


def configure_connection(db):
    db.row_factory = sqlite3.Row
    db.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
    db.create_function("phonoteka_schema_version", 0, lambda: SCHEMA_VERSION, deterministic=True)
    db.create_function("phonoteka_audit_filter", 4, audit_status.matches, deterministic=True)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA synchronous=FULL")
    return db


def connect(path: Path, *, maintenance=False) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=15, factory=ClosingConnection)
    try:
        configure_connection(db)
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if not maintenance:
            if version==1:raise RuntimeError('Schema 1 requires offline migration: stop app/worker, run python -m phonoteka.migrations migrate --confirm-stopped')
            if version not in {0,SCHEMA_VERSION}:raise RuntimeError('Unsupported database schema; compatible image required')
            marker=Path(path).parent/'.schema-migration.json'
            if marker.exists() and json.loads(marker.read_text()).get('state')!='committed':
                raise RuntimeError('Migration recovery required: inspect python -m phonoteka.migrations status before restarting')
        return db
    except BaseException:
        db.close();raise


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


def initialize(path: Path, *, settings=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, SCHEMA_VERSION}:
            raise RuntimeError("Unsupported database schema; compatible image required")
        if version==SCHEMA_VERSION:
            verify_schema(db)
            return
        db.execute('BEGIN IMMEDIATE')
        create_base_schema(db)
        install_schema_guard(db,0,None)
        db.execute('PRAGMA user_version=2')
        db.commit()


def base_schema_sql():
    script="""
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
        CREATE TABLE IF NOT EXISTS tracks_search (
            track_id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            document TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS tracks_search_insert AFTER INSERT ON tracks BEGIN
            INSERT INTO tracks_search VALUES(NEW.id,casefold(NEW.relative_path || char(31) ||
              COALESCE((SELECT group_concat(atom,char(31)) FROM json_tree(NEW.metadata,'$.tags') WHERE type='text'),'') || char(31) ||
              COALESCE((SELECT group_concat(atom,char(31)) FROM json_tree(NEW.metadata,'$.cue.tracks') WHERE type='text'),'')));
        END;
        CREATE TRIGGER IF NOT EXISTS tracks_search_update AFTER UPDATE OF relative_path,metadata ON tracks BEGIN
            INSERT INTO tracks_search VALUES(NEW.id,casefold(NEW.relative_path || char(31) ||
              COALESCE((SELECT group_concat(atom,char(31)) FROM json_tree(NEW.metadata,'$.tags') WHERE type='text'),'') || char(31) ||
              COALESCE((SELECT group_concat(atom,char(31)) FROM json_tree(NEW.metadata,'$.cue.tracks') WHERE type='text'),'')))
              ON CONFLICT(track_id) DO UPDATE SET document=excluded.document;
        END;
        CREATE TRIGGER IF NOT EXISTS tracks_search_delete AFTER DELETE ON tracks BEGIN
            DELETE FROM tracks_search WHERE track_id=OLD.id;
        END;
        INSERT OR IGNORE INTO tracks_search SELECT id,casefold(relative_path || char(31) ||
          COALESCE((SELECT group_concat(atom,char(31)) FROM json_tree(metadata,'$.tags') WHERE type='text'),'') || char(31) ||
          COALESCE((SELECT group_concat(atom,char(31)) FROM json_tree(metadata,'$.cue.tracks') WHERE type='text'),'')) FROM tracks;
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
        CREATE TABLE IF NOT EXISTS trace_attempts (
            job_id TEXT NOT NULL REFERENCES jobs(id), item_id TEXT NOT NULL,
            number INTEGER NOT NULL, context TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(job_id,item_id)
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
        CREATE TABLE IF NOT EXISTS webhook_outbox (
            id TEXT PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE, destination_id TEXT NOT NULL,
            job_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
            lease_token TEXT, leased_until REAL, last_code TEXT, delivered_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS webhook_pending ON webhook_outbox(state,next_attempt,event_id);
        CREATE TABLE IF NOT EXISTS webhook_reservations (job_id TEXT PRIMARY KEY,destination_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS analyses (
            track_id TEXT PRIMARY KEY REFERENCES tracks(id), input_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_cache (
            key TEXT PRIMARY KEY, provider TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_calls (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS provider_calls_time ON provider_calls(provider,at);
        CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL, scopes TEXT NOT NULL,
            created REAL NOT NULL, expires REAL NOT NULL, last_used REAL, revoked INTEGER NOT NULL DEFAULT 0,
            auth_epoch INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS delivery_receipts (id TEXT PRIMARY KEY, export_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        """
    return script


def schema_checksum():
    return hashlib.sha256((base_schema_sql()+'v2:all-table-insert-update-delete:phonoteka_schema_version()==2').encode()).hexdigest()


def create_base_schema(db):
    statement=''
    script=base_schema_sql()
    for line in script.splitlines():
        statement+=line+'\n'
        if sqlite3.complete_statement(statement):
            db.execute(statement);statement=''
    if statement.strip():raise RuntimeError('Incomplete schema migration')
    db.execute("INSERT OR IGNORE INTO settings VALUES('auth_epoch','1')")
    db.execute("INSERT OR IGNORE INTO settings VALUES('instance_uuid',?)", (str(uuid.uuid4()),))


def install_schema_guard(db,source_version,checkpoint_id):
    db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,source_version INTEGER NOT NULL,migration_id TEXT NOT NULL,checksum TEXT NOT NULL,checkpoint_id TEXT,applied_at TEXT NOT NULL)')
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        table=row[0]
        if not table.replace('_','').isalnum():raise RuntimeError('Unexpected schema identifier')
        for operation in ('INSERT','UPDATE','DELETE'):
            db.execute(f'CREATE TRIGGER IF NOT EXISTS schema2_{table}_{operation.lower()} BEFORE {operation} ON "{table}" BEGIN SELECT CASE WHEN phonoteka_schema_version()!=2 THEN RAISE(ABORT,\'schema_write_guard\') END; END')
    db.execute('INSERT INTO schema_migrations VALUES(?,?,?,?,?,?) ON CONFLICT(version) DO NOTHING',(2,source_version,'v1-to-v2-write-barrier',schema_checksum(),checkpoint_id,utcnow()))


def verify_schema(db):
    row=db.execute('SELECT migration_id,checksum FROM schema_migrations WHERE version=2').fetchone()
    if not row or row[0]!='v1-to-v2-write-barrier' or row[1]!=schema_checksum():raise RuntimeError('Schema migration ledger is inconsistent')
    tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    triggers={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if any(f'schema2_{table}_{operation}' not in triggers for table in tables for operation in ('insert','update','delete')):
        raise RuntimeError('Schema write barrier is incomplete; maintenance repair required')


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
    created_at=utcnow()
    cursor=db.execute("INSERT INTO events(kind,payload,created_at) VALUES(?,?,?)", (kind,encode(payload),created_at))
    # This hook shares the caller's transaction. A process crash cannot leave a
    # committed result event without its enabled terminal-job notification.
    from .webhooks import enqueue
    enqueue(db,cursor.lastrowid,kind,payload,created_at)
    # Both time and count cap replay; clients outside retained IDs explicitly resync.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="milliseconds")
    db.execute("DELETE FROM events WHERE id <= COALESCE((SELECT MAX(id)-10000 FROM events),0) OR created_at < ?", (cutoff,))
