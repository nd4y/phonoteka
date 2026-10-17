"""HTTP API and static UI. This process never writes music files."""

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import time
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from urllib.parse import urlsplit

from . import __version__, db, security
from .config import Settings


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: dict[str, str] = Field(default_factory=dict)
    lrc: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    cover: str | None = Field(default=None, max_length=14 * 1024 * 1024)
    base_proposal_id: str | None = Field(default=None, min_length=1, max_length=36)


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["scan", "audit", "apply", "rollback", "publish"]
    track_ids: list[str] = Field(default_factory=list, max_length=10000)
    params: dict = Field(default_factory=dict)


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes=15 * 1024 * 1024):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        received = 0
        async def limited_receive():
            nonlocal received
            msg = await receive()
            received += len(msg.get("body", b""))
            if received > self.max_bytes:
                raise security.error(413, "payload_too_large", "Размер запроса превышает лимит")
            return msg
        await self.app(scope, limited_receive, send)


def _track_row(settings: Settings, track_id: str):
    row = db.fetchone(settings.db_path, "SELECT * FROM tracks WHERE id=? AND is_current=1", (track_id,))
    if not row:
        raise security.error(404, "track_not_found", "Трек не найден", file_id=track_id)
    return row


def _public_proposal(row):
    if not row:
        return None
    payload = json.loads(row["payload"])
    if payload.get("cover"):
        payload["cover"] = {"present": True, "mime": payload["cover"].split(";", 1)[0][5:]}
    return {"id": row["id"], "track_id": row["track_id"], "input_sha256": row["input_sha256"], "created_at": row["created_at"], **payload}


def public_track(settings: Settings, row: dict, detail=False):
    meta = json.loads(row["metadata"])
    proposal = db.fetchone(settings.db_path, "SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["id"],))
    result = {
        **meta, "id": row["id"], "relative_path": row["relative_path"],
        "filename": Path(row["relative_path"]).name,
        "sha256": row["sha256"], "revision": row["sha256"], "status": row["status"],
        "can_write": settings.writable and bool(meta.get("write_supported", meta.get("format", "").lower() in {"mp3", "flac"})) and row["status"] != "missing",
        "tags": {key: (value[0] if value else "") if isinstance(value, list) else value for key, value in meta.get("tags", {}).items()},
        "tag_values": meta.get("tags", {}), "has_art": bool(meta.get("has_art", False)),
        "lyrics": meta.get("lyrics", ""), "lrc": meta.get("lrc", ""),
        "proposal": _public_proposal(proposal), "updated_at": row["updated_at"],
    }
    if detail:
        rows = db.fetchall(settings.db_path, "SELECT * FROM history WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (row["id"],))
        result["history"] = [public_history(item) for item in rows]
    return result


def public_history(row):
    details = json.loads(row["details"])
    # Paths of backups/journals are internal worker handles, never user input.
    safe = {key: val for key, val in details.items() if key not in {"backup_path", "journal_path", "target", "source", "destination"}}
    return {key: row[key] for key in ("id", "track_id", "job_id", "kind", "before_sha256", "after_sha256", "created_at")} | {
        "details": safe, "rollback_available": bool(details.get("backup_path")) and row["kind"] in {"apply", "rollback"}
    }


def public_job(settings: Settings, row: dict, detail=False):
    total = row["total"]
    result = {key: row[key] for key in ("id", "kind", "state", "stage", "total", "completed", "failed", "cancel_requested", "created_at", "updated_at", "error")}
    result["progress"] = round(100 * (row["completed"] + row["failed"]) / total) if total else 0
    result["cancel_requested"] = bool(row["cancel_requested"])
    if detail:
        items = db.fetchall(settings.db_path, "SELECT item_key,state,result FROM job_items WHERE job_id=? ORDER BY item_key LIMIT 10000", (row["id"],))
        result["results"] = [{"track_id": item["item_key"], "state": item["state"], **json.loads(item["result"])} for item in items]
    return result


def resolve_track_path(settings: Settings, row: dict) -> Path:
    relative = Path(row["relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise security.error(409, "unsafe_path", "Некорректный путь трека")
    root = settings.work_root.resolve(strict=True)
    target = root / relative
    part = root
    for segment in relative.parts:
        part /= segment
        if part.is_symlink():
            raise security.error(409, "unsafe_path", "Ссылки в пути не поддерживаются")
    if not target.is_file() or not target.resolve().is_relative_to(root):
        raise security.error(404, "file_unavailable", "Файл недоступен")
    return target


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        await asyncio.to_thread(db.initialize, settings.db_path)
        await asyncio.to_thread(security.bootstrap, settings)
        yield

    app = FastAPI(title="Фонотека API", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.app_url).hostname, "localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "private, no-store"
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        data = exc.detail if isinstance(exc.detail, dict) else {"code": "request_failed", "message": str(exc.detail), "retryable": False}
        headers = dict(exc.headers or {})
        if "retry_after" in data:
            headers["Retry-After"] = str(data["retry_after"])
        return JSONResponse(data, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default validation output can include the submitted password.
        return JSONResponse({"code": "validation_error", "message": "Некорректный запрос", "retryable": False,
                             "fields": [".".join(str(part) for part in err["loc"]) for err in exc.errors()]}, status_code=422)

    @app.get("/health")
    def health():
        db.fetchone(settings.db_path, "SELECT 1")
        return {"status": "ok", "version": __version__}

    @app.get("/api/auth/session")
    def auth_session(request: Request):
        active = security.session(request, settings)
        return {"authenticated": bool(active), "csrf_token": active["csrf"] if active else None,
                "username": settings.owner_username if active else None}

    @app.post("/api/auth/login")
    def auth_login(request: Request, response: Response, body: LoginInput):
        token, csrf = security.login(request, settings, body.username, body.password)
        response.set_cookie(security.cookie_name(settings), token, httponly=True, secure=settings.secure_cookie,
                            samesite="strict", path="/", max_age=settings.session_hours * 3600)
        return {"authenticated": True, "csrf_token": csrf, "username": settings.owner_username}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        active = security.require_session(request, settings, True)
        with db.transaction(settings.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (active["token_hash"],))
        response.delete_cookie(security.cookie_name(settings), path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return {"authenticated": False, "csrf_token": None, "username": None}

    @app.get("/api/summary")
    def summary(request: Request):
        security.require_session(request, settings)
        rows = db.fetchall(settings.db_path, "SELECT metadata,status,sha256 FROM tracks WHERE is_current=1")
        count = {"total": len(rows), "missing_tags": 0, "missing_art": 0, "missing_lyrics": 0, "needs_review": 0, "published": 0}
        formats = {}
        for row in rows:
            meta = json.loads(row["metadata"])
            fmt = meta.get("format", "unknown")
            formats[fmt] = formats.get(fmt, 0) + 1
            count["missing_tags"] += not (meta.get("tags", {}).get("title") and meta.get("tags", {}).get("artist"))
            count["missing_art"] += not meta.get("has_art", False)
            count["missing_lyrics"] += not meta.get("lrc", "")
            count["needs_review"] += row["status"] in {"needs_review", "conflict", "error", "missing"}
            count["published"] += meta.get("publication", {}).get("sha256") == row["sha256"]
        active = db.fetchone(settings.db_path, "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','running','waiting','interrupted')")["n"]
        heartbeat = json.loads(db.setting(settings.db_path, "worker_heartbeat", "{}"))
        return {**count, "formats": formats, "active_jobs": active, "read_only": not settings.writable,
                "worker": {**heartbeat, "online": time.time()-heartbeat.get("at", 0) < 30 and heartbeat.get("state") != "stopped"},
                "capabilities": {"edit": True, "apply": settings.writable, "publish": settings.writable, "audit": True, "recognition": False, "asr": False},
                "roots": {"work": str(settings.work_root)}, "version": __version__}

    @app.get("/api/tracks")
    def tracks(request: Request, search: str = Query("", max_length=200), page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200), filter: str = Query("all")):
        security.require_session(request, settings)
        clauses = ["t.is_current=1"]
        args = []
        if search:
            clauses.append("(LOWER(t.relative_path) LIKE LOWER(?) ESCAPE '\\' OR LOWER(t.metadata) LIKE LOWER(?) ESCAPE '\\')")
            term = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [term, term]
        filters = {
            "all": "1=1",
            "review": "(t.status IN ('needs_review','conflict','error','missing') OR EXISTS (SELECT 1 FROM proposals p WHERE p.track_id=t.id AND p.input_sha256=t.sha256))",
            "missing_lyrics": "COALESCE(json_extract(t.metadata,'$.lrc'),'')=''",
            "missing_art": "COALESCE(json_extract(t.metadata,'$.has_art'),0)=0",
            "published": "json_extract(t.metadata,'$.publication.sha256')=t.sha256",
        }
        if filter not in filters:
            raise security.error(422, "invalid_filter", "Неизвестный фильтр")
        clauses.append(filters[filter])
        where = " AND ".join(clauses)
        total = db.fetchone(settings.db_path, f"SELECT COUNT(*) AS n FROM tracks t WHERE {where}", args)["n"]
        rows = db.fetchall(settings.db_path, f"SELECT t.* FROM tracks t WHERE {where} ORDER BY t.relative_path,t.id LIMIT ? OFFSET ?", (*args, page_size, (page-1)*page_size))
        return {"items": [public_track(settings, row) for row in rows], "total": total, "page": page, "page_size": page_size}

    @app.get("/api/tracks/{track_id}")
    def track(request: Request, response: Response, track_id: str):
        security.require_session(request, settings)
        row = _track_row(settings, track_id)
        response.headers["ETag"] = '"' + row["sha256"] + '"'
        return public_track(settings, row, True)

    @app.post("/api/tracks/{track_id}/proposal", status_code=201)
    def proposal(request: Request, track_id: str, body: ProposalInput):
        security.require_session(request, settings, True)
        match = request.headers.get("if-match")
        if not match:
            raise security.error(428, "precondition_required", "Передайте текущую ревизию If-Match")
        if set(body.tags) - {"title", "artist", "album", "date", "tracknumber"}:
            raise security.error(422, "unsupported_tag", "Поле тега не поддерживается")
        if any(len(value) > 4096 or "\x00" in value for value in body.tags.values()):
            raise security.error(422, "invalid_tag", "Недопустимое значение тега")
        if body.cover:
            if not re.match(r"^data:image/(jpeg|png);base64,", body.cover):
                raise security.error(422, "invalid_cover", "Обложка должна быть JPEG или PNG")
            try:
                data = base64.b64decode(body.cover.split(",", 1)[1], validate=True)
            except ValueError:
                raise security.error(422, "invalid_cover", "Неверный формат обложки")
            if len(data) > 10 * 1024 * 1024:
                raise security.error(413, "cover_too_large", "Обложка превышает 10 МиБ")
        now, identity = db.utcnow(), str(uuid.uuid4())
        payload = body.model_dump(exclude_unset=True, exclude={"base_proposal_id"})
        with db.transaction(settings.db_path) as conn:
            row = conn.execute("SELECT sha256 FROM tracks WHERE id=? AND is_current=1", (track_id,)).fetchone()
            if not row:
                raise security.error(404, "track_not_found", "Трек не найден")
            if match.strip('"') != row["sha256"]:
                raise security.error(412, "revision_conflict", "Файл изменился. Обновите карточку")
            if body.base_proposal_id:
                base = conn.execute("SELECT * FROM proposals WHERE id=? AND track_id=?", (body.base_proposal_id,track_id)).fetchone()
                latest = conn.execute("SELECT id FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (track_id,)).fetchone()
                if not base or base["input_sha256"] != row["sha256"] or not latest or latest["id"] != body.base_proposal_id:
                    raise security.error(412, "proposal_conflict", "Черновик изменился. Обновите карточку")
                previous = json.loads(base["payload"])
                merged_tags = {**previous.get("tags", {}), **payload.get("tags", {})}
                payload = {**previous, **payload, "tags": merged_tags}
            conn.execute("INSERT INTO proposals VALUES(?,?,?,?,?)", (identity,track_id,row["sha256"],db.encode(payload),now))
            conn.execute("UPDATE tracks SET status='needs_review',updated_at=? WHERE id=?", (now,track_id))
            db.event(conn, "proposal_created", {"track_id": track_id, "proposal_id": identity})
        return _public_proposal(db.fetchone(settings.db_path, "SELECT * FROM proposals WHERE id=?", (identity,)))

    @app.get("/api/tracks/{track_id}/audio")
    def audio(request: Request, track_id: str):
        security.require_session(request, settings)
        # Keep Range work bounded, including old Starlette multi-range parsers.
        range_header = request.headers.get("range", "")
        if "," in range_header or len(range_header) > 100:
            raise security.error(416, "unsupported_range", "Поддерживается один диапазон байтов")
        path = resolve_track_path(settings, _track_row(settings, track_id))
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", headers={"Cache-Control": "private, no-store"})

    @app.get("/api/tracks/{track_id}/art")
    def art(request: Request, track_id: str):
        security.require_session(request, settings)
        from .media import read_art
        content = read_art(resolve_track_path(settings, _track_row(settings, track_id)), {"work": settings.work_root, "source": settings.source_root})
        if not content:
            raise security.error(404, "art_not_found", "Обложка отсутствует")
        data, mime = content
        return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store"})

    @app.get("/api/jobs")
    def jobs(request: Request, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
        security.require_session(request, settings)
        rows = db.fetchall(settings.db_path, "SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (page_size,(page-1)*page_size))
        total = db.fetchone(settings.db_path, "SELECT COUNT(*) AS n FROM jobs")["n"]
        return {"items": [public_job(settings, row) for row in rows], "total": total, "page": page, "page_size": page_size}

    @app.get("/api/jobs/{job_id}")
    def get_job(request: Request, job_id: str):
        security.require_session(request, settings)
        row = db.fetchone(settings.db_path, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise security.error(404, "job_not_found", "Задание не найдено")
        return public_job(settings, row, True)

    @app.post("/api/jobs", status_code=202)
    def create_job(request: Request, body: JobInput):
        active = security.require_session(request, settings, True)
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 128:
            raise security.error(428, "idempotency_key_required", "Передайте Idempotency-Key длиной до 128 символов")
        if body.kind in {"apply", "rollback", "publish"} and not settings.writable:
            raise security.error(409, "read_only", "Запись отключена до проверки конфигурации хранилища")
        if set(body.params) - {"proposal_id", "history_id", "allow_incomplete", "exception_reason"}:
            raise security.error(422, "invalid_params", "Неизвестный параметр задания")
        if body.params.get("allow_incomplete") and (not isinstance(body.params.get("exception_reason"), str) or not 5 <= len(body.params["exception_reason"]) <= 1000):
            raise security.error(422, "exception_reason_required", "Укажите причину публикации неполных метаданных")
        ids = list(dict.fromkeys(body.track_ids))
        if body.kind == "scan" and ids:
            raise security.error(422, "invalid_scope", "Сканирование выполняется для всей библиотеки")
        if body.kind != "scan" and not ids:
            raise security.error(422, "tracks_required", "Выберите конкретные треки")
        request_hash = hashlib.sha256(db.encode(body.model_dump()).encode()).hexdigest()
        now, identity = db.utcnow(), str(uuid.uuid4())
        with db.transaction(settings.db_path) as conn:
            previous = conn.execute("SELECT * FROM idempotency WHERE scope='owner:jobs' AND key=?", (key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise security.error(409, "idempotency_conflict", "Этот ключ уже использован для другого запроса")
                existing = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (previous["job_id"],)).fetchone())
                return public_job(settings, existing)
            items = []
            for track_id in ids:
                track = conn.execute("SELECT * FROM tracks WHERE id=? AND is_current=1", (track_id,)).fetchone()
                if not track:
                    raise security.error(404, "track_not_found", "Трек не найден", file_id=track_id)
                item = {"track_id": track_id, "sha256": track["sha256"], "relative_path": track["relative_path"]}
                if body.kind == "apply":
                    if body.params.get("proposal_id"):
                        prop = conn.execute("SELECT * FROM proposals WHERE id=? AND track_id=?", (body.params["proposal_id"],track_id)).fetchone()
                    else:
                        prop = conn.execute("SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (track_id,)).fetchone()
                    if not prop or prop["input_sha256"] != track["sha256"]:
                        raise security.error(409, "proposal_stale", "Создайте актуальное предложение", file_id=track_id)
                    item["proposal_id"] = prop["id"]
                    item["proposal"] = json.loads(prop["payload"])
                if body.kind == "rollback":
                    if body.params.get("history_id"):
                        history = conn.execute("SELECT * FROM history WHERE id=? AND track_id=?", (body.params["history_id"],track_id)).fetchone()
                    else:
                        history = conn.execute("SELECT * FROM history WHERE track_id=? AND kind='apply' ORDER BY created_at DESC,rowid DESC LIMIT 1", (track_id,)).fetchone()
                    if not history or history["after_sha256"] != track["sha256"] or not json.loads(history["details"]).get("backup_path"):
                        raise security.error(409, "rollback_conflict", "Нет доступной резервной версии для текущего файла", file_id=track_id)
                    item["history_id"] = history["id"]
                items.append(item)
            payload = {"items": items, "params": body.params}
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,?,'queued','pending',?,?,?,?,?)", (identity,body.kind,db.encode(payload),len(items),now,now,active["auth_epoch"]))
            conn.execute("INSERT INTO idempotency VALUES('owner:jobs',?,?,?,?)", (key,request_hash,identity,now))
            db.event(conn, "job_created", {"job_id": identity, "kind": body.kind})
            row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone())
        return public_job(settings, row)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(request: Request, job_id: str):
        security.require_session(request, settings, True)
        with db.transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise security.error(404, "job_not_found", "Задание не найдено")
            if row["state"] in {"queued", "waiting", "paused"}:
                conn.execute("UPDATE jobs SET state='cancelled',stage='cancelled',cancel_requested=1,updated_at=? WHERE id=?", (db.utcnow(),job_id))
            elif row["state"] == "running":
                conn.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (db.utcnow(),job_id))
            db.event(conn, "job_cancel_requested", {"job_id": job_id})
        return public_job(settings, db.fetchone(settings.db_path, "SELECT * FROM jobs WHERE id=?", (job_id,)))

    @app.get("/api/events")
    async def events(request: Request):
        await asyncio.to_thread(security.require_session, request, settings)
        try:
            after = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise security.error(422, "invalid_event_id", "Неверный идентификатор события")
        async def stream():
            nonlocal after
            while not await request.is_disconnected():
                if not await asyncio.to_thread(security.session, request, settings):
                    break
                boundary = await asyncio.to_thread(db.fetchone, settings.db_path, "SELECT MIN(id) AS first,MAX(id) AS last FROM events")
                if after and boundary["first"] and after < boundary["first"]-1:
                    after = boundary["last"] or 0
                    yield f"id: {after}\nevent: resync_required\ndata: {{}}\n\n"
                rows = await asyncio.to_thread(db.fetchall, settings.db_path, "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100", (after,))
                for row in rows:
                    after = row["id"]
                    yield f"id: {row['id']}\nevent: {row['kind']}\ndata: {row['payload']}\n\n"
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "private, no-store"})

    @app.get("/api/openapi.json")
    def openapi(request: Request):
        security.require_session(request, settings)
        return app.openapi()

    # Versioned production contract and temporary alpha frontend alias share handlers.
    for route in list(app.routes):
        if getattr(route, "path", "").startswith("/api/"):
            app.add_api_route("/api/v1/" + route.path[len("/api/"):], route.endpoint,
                              methods=list(route.methods), status_code=route.status_code,
                              include_in_schema=False, name=route.name + "_v1")
    if settings.static_dir and settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")
    return app


app = create_app()
