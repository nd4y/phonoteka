"""One owner, revocable server sessions, synchronizer CSRF, durable throttling."""

import hashlib
import json
import math
import re
import secrets
import time
import threading
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import HTTPException, Request

from . import db
from .config import Settings

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
VERIFY_SLOTS = threading.BoundedSemaphore(2)
SESSION_ACTIVITY_PREFIX = 'session_activity:'
SESSION_ACTIVITY_INTERVAL = 30


def error(status: int, code: str, message: str, **extra):
    return HTTPException(status, {"code": code, "message": message, "retryable": False, **extra})


def bootstrap(settings: Settings) -> None:
    # Persist policy reductions before serving requests. A later configuration
    # increase must not bring an already expired cookie back to life.
    with db.transaction(settings.db_path) as conn:
        _prune_sessions(conn, settings)
    previous = db.setting(settings.db_path, "owner_password_hash")
    file = settings.owner_password_file
    if not file or not file.is_file():
        if previous:
            return
        raise RuntimeError("OWNER_PASSWORD_FILE is required for the first start")
    password = file.read_text(encoding="utf-8").rstrip("\r\n")
    if len(password) < 16 or len(password) > 1024:
        raise RuntimeError("Owner password must contain 16..1024 characters")
    same = False
    if previous:
        try:
            same = HASHER.verify(previous, password)
        except (VerifyMismatchError, VerificationError):
            pass
    if same and not HASHER.check_needs_rehash(previous):
        return
    hashed = HASHER.hash(password)
    with db.transaction(settings.db_path) as conn:
        conn.execute("INSERT INTO settings VALUES('owner_password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (hashed,))
        if previous and not same:
            conn.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_epoch'")
            revoke_sessions(conn)
            db.event(conn, "auth_reset", {})


def cookie_name(settings: Settings):
    return "__Host-phonoteka" if settings.secure_cookie else "phonoteka-dev"


def token_hash(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _activity_value(value):
    if not isinstance(value, str) or len(value) > 512:
        return None
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {'version', 'created', 'last_activity', 'idle_expires', 'auth_epoch'}:
        return None
    if type(data['version']) is not int or data['version'] != 1 or type(data['auth_epoch']) is not int:
        return None
    if any(not _finite_number(data[key]) for key in ('created', 'last_activity', 'idle_expires')):
        return None
    return data


def _session_row(conn, digest):
    row = conn.execute("""SELECT s.*, a.value AS activity_value, e.value AS current_epoch
        FROM sessions s LEFT JOIN settings a ON a.key=? || s.token_hash
        LEFT JOIN settings e ON e.key='auth_epoch' WHERE s.token_hash=?""",
        (SESSION_ACTIVITY_PREFIX, digest)).fetchone()
    return dict(row) if row else None


def _valid_session(row, settings, now):
    if not row or not isinstance(row['current_epoch'], str) or len(row['current_epoch']) > 20:
        return None
    try:
        epoch = int(row['current_epoch'])
    except (TypeError, ValueError):
        return None
    activity = _activity_value(row['activity_value'])
    if not activity or row['auth_epoch'] != epoch or activity['auth_epoch'] != epoch:
        return None
    if any(not _finite_number(row[key]) for key in ('created', 'expires')):
        return None
    if not row['created'] == activity['created'] <= activity['last_activity'] <= now:
        return None
    absolute = min(row['expires'], row['created'] + settings.session_absolute_seconds)
    idle = min(activity['idle_expires'], activity['last_activity'] + settings.session_idle_seconds, absolute)
    if now >= idle or now >= absolute:
        return None
    result = {key: row[key] for key in ('token_hash', 'csrf', 'created', 'expires', 'auth_epoch')}
    return {**result, 'idle_expires_at': idle, 'absolute_expires_at': absolute, '_activity': activity}


def _cleanup_activity(conn):
    # Exact prefix, not LIKE: underscores in this private namespace are literal.
    conn.execute("""DELETE FROM settings WHERE substr(key,1,?)=?
        AND substr(key,?) NOT IN (SELECT token_hash FROM sessions)""",
        (len(SESSION_ACTIVITY_PREFIX), SESSION_ACTIVITY_PREFIX, len(SESSION_ACTIVITY_PREFIX) + 1))


def revoke_sessions(conn, digest=None):
    if digest is None:
        conn.execute('DELETE FROM sessions')
        conn.execute('DELETE FROM settings WHERE substr(key,1,?)=?', (len(SESSION_ACTIVITY_PREFIX), SESSION_ACTIVITY_PREFIX))
    else:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))
        conn.execute('DELETE FROM settings WHERE key=?', (SESSION_ACTIVITY_PREFIX + digest,))


def _prune_sessions(conn, settings):
    conn.execute('DELETE FROM sessions WHERE token_hash NOT IN (SELECT token_hash FROM sessions ORDER BY created DESC,rowid DESC LIMIT 20)')
    for item in conn.execute('SELECT token_hash FROM sessions').fetchall():
        row = _session_row(conn, item['token_hash'])
        active = _valid_session(row, settings, time.time())
        if not active:
            revoke_sessions(conn, item['token_hash'])
            continue
        activity = active['_activity']
        # Startup/login can shorten stored deadlines, never extend them.
        if activity['idle_expires'] != active['idle_expires_at']:
            activity = {**activity, 'idle_expires': active['idle_expires_at']}
            conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(activity), SESSION_ACTIVITY_PREFIX + item['token_hash']))
        if row['expires'] != active['absolute_expires_at']:
            conn.execute('UPDATE sessions SET expires=? WHERE token_hash=?', (active['absolute_expires_at'], item['token_hash']))
    _cleanup_activity(conn)


def session_by_hash(settings: Settings, digest: str):
    if not re.fullmatch('[0-9a-f]{64}', digest):
        return None
    with db.connect(settings.db_path) as conn:
        row = _session_row(conn, digest)
        active = _valid_session(row, settings, time.time())
    if active:
        return active
    if row:
        # Expiry may be observed between policy restarts or a concurrent touch.
        # Revalidate under the write lock; deletion never renews a session.
        with db.transaction(settings.db_path) as conn:
            if not _valid_session(_session_row(conn, digest), settings, time.time()):
                revoke_sessions(conn, digest)
    return None


def public_session(settings: Settings, active):
    return {'authenticated': bool(active), 'csrf_token': active['csrf'] if active else None,
            'username': settings.owner_username if active else None,
            'idle_expires_at': active['idle_expires_at'] if active else None,
            'absolute_expires_at': active['absolute_expires_at'] if active else None,
            'activity_interval_seconds': SESSION_ACTIVITY_INTERVAL}


def session(request: Request, settings: Settings):
    token = request.cookies.get(cookie_name(settings), "")
    if not token or len(token) > 256:
        return None
    return session_by_hash(settings, token_hash(token))


def record_activity(request: Request, settings: Settings):
    active = require_session(request, settings, True, owner_only=True)
    if request.headers.get('origin') != settings.app_url:
        raise error(403, 'origin_denied', 'User activity requires the application origin')
    with db.transaction(settings.db_path) as conn:
        row = _session_row(conn, active['token_hash'])
        now = time.time()
        fresh = _valid_session(row, settings, now)
        if not fresh:
            raise error(401, 'authentication_required', 'Session expired; sign in again')
        activity = {**fresh['_activity'], 'last_activity': now,
                    'idle_expires': min(now + settings.session_idle_seconds, fresh['absolute_expires_at'])}
        changed = conn.execute('UPDATE settings SET value=? WHERE key=?',
            (db.encode(activity), SESSION_ACTIVITY_PREFIX + fresh['token_hash'])).rowcount
        if changed != 1:
            raise error(401, 'authentication_required', 'Session expired; sign in again')
        return _valid_session(_session_row(conn, fresh['token_hash']), settings, now)


def require_session(request: Request, settings: Settings, mutation=False, *, owner_only=False):
    if request.headers.get('authorization'):
        if owner_only:raise error(403,'owner_session_required','Нужна сессия владельца в браузере')
        from .automation import bearer
        return bearer(request,settings)
    active = session(request, settings)
    if not active:
        raise error(401, "authentication_required", "Войдите в приложение")
    if mutation:
        origin = request.headers.get("origin")
        if origin and origin != settings.app_url:
            raise error(403, "origin_denied", "Источник запроса не разрешен")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise error(403, "origin_denied", "Межсайтовый запрос не разрешен")
        csrf = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(csrf, active["csrf"]):
            raise error(403, "csrf_invalid", "Обновите страницу и повторите запрос")
    return active


def login(request: Request, settings: Settings, username: str, password: str):
    if request.headers.get("origin") != settings.app_url or request.headers.get("sec-fetch-site") == "cross-site":
        raise error(403, "origin_denied", "Источник запроса не разрешен")
    # Forwarded headers are deliberately not trusted. At an internal proxy this is
    # a conservative global origin throttle, not a spoofable X-Forwarded-For limit.
    client = request.client.host if request.client else "unknown"
    now = time.time()
    with db.transaction(settings.db_path) as conn:
        conn.execute("DELETE FROM login_attempts WHERE at<?", (now-300,))
        attempts = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE client=?", (client,)).fetchone()[0]
        global_attempts = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE at>?", (now-60,)).fetchone()[0]
        if attempts >= 8 or global_attempts >= 20:
            raise error(429, "login_rate_limited", "Слишком много попыток входа. Повторите позже", retry_after=300)
        conn.execute("INSERT INTO login_attempts(client,at) VALUES(?,?)", (client,now))
    hashed = db.setting(settings.db_path, "owner_password_hash", "")
    good_password = False
    if not VERIFY_SLOTS.acquire(blocking=False):
        raise error(429, "login_busy", "Проверка входа занята. Повторите через несколько секунд", retry_after=2)
    try:
        good_password = HASHER.verify(hashed, password)
    except (VerifyMismatchError, VerificationError):
        pass
    finally:
        VERIFY_SLOTS.release()
    if not good_password or not secrets.compare_digest(username.encode(), settings.owner_username.encode()):
        raise error(401, "invalid_credentials", "Неверный логин или пароль")
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with db.transaction(settings.db_path) as conn:
        _prune_sessions(conn, settings)
        current_hash = conn.execute("SELECT value FROM settings WHERE key='owner_password_hash'").fetchone()
        if not current_hash or not secrets.compare_digest(current_hash['value'], hashed):
            raise error(401, 'invalid_credentials', 'Credentials changed; sign in again')
        epoch = int(conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone()['value'])
        now = time.time()
        conn.execute("DELETE FROM login_attempts WHERE client=?", (client,))
        digest = token_hash(token)
        absolute = now + settings.session_absolute_seconds
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (digest,csrf,now,absolute,epoch))
        conn.execute('INSERT INTO settings VALUES(?,?)', (SESSION_ACTIVITY_PREFIX + digest,
            db.encode({'version': 1, 'created': now, 'last_activity': now,
                       'idle_expires': min(now + settings.session_idle_seconds, absolute), 'auth_epoch': epoch})))
        # Bound owner sessions; a new login cannot grow state indefinitely.
        conn.execute("""DELETE FROM sessions WHERE token_hash<>? AND token_hash NOT IN
            (SELECT token_hash FROM sessions WHERE token_hash<>? ORDER BY created DESC,rowid DESC LIMIT 19)""", (digest, digest))
        _cleanup_activity(conn)
    return token, csrf
