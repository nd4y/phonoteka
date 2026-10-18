"""One owner, revocable server sessions, synchronizer CSRF, durable throttling."""

import hashlib
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


def error(status: int, code: str, message: str, **extra):
    return HTTPException(status, {"code": code, "message": message, "retryable": False, **extra})


def bootstrap(settings: Settings) -> None:
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
            conn.execute("DELETE FROM sessions")
            db.event(conn, "auth_reset", {})


def cookie_name(settings: Settings):
    return "__Host-phonoteka" if settings.secure_cookie else "phonoteka-dev"


def token_hash(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


def session(request: Request, settings: Settings):
    token = request.cookies.get(cookie_name(settings), "")
    if not token or len(token) > 256:
        return None
    row = db.fetchone(settings.db_path, "SELECT * FROM sessions WHERE token_hash=? AND expires>?", (token_hash(token), time.time()))
    if row and row["auth_epoch"] == int(db.setting(settings.db_path, "auth_epoch", "0")):
        return row
    return None


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
    epoch = int(db.setting(settings.db_path, "auth_epoch"))
    with db.transaction(settings.db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE expires<?", (now,))
        conn.execute("DELETE FROM login_attempts WHERE client=?", (client,))
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (token_hash(token),csrf,now,now+settings.session_hours*3600,epoch))
        # Bound owner sessions; a new login cannot grow state indefinitely.
        conn.execute("DELETE FROM sessions WHERE token_hash NOT IN (SELECT token_hash FROM sessions ORDER BY created DESC LIMIT 20)")
    return token, csrf
