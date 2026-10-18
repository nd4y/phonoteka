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

from . import __version__, db, security, cue, reports, intake
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
    kind: Literal["scan", "audit", "apply", "rollback", "publish", "analyze", "recognize", "transcribe", "align", "extract_cue", "replaygain", "replaygain_album", "export_ready", "normalize_audio"]
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
        "can_write": settings.writable and not meta.get('cue_album_image') and bool(meta.get("write_supported", meta.get("format", "").lower() in {"mp3", "flac"})) and row["status"] != "missing",
        "tags": {key: (value[0] if value else "") if isinstance(value, list) else value for key, value in meta.get("tags", {}).items()},
        "tag_values": meta.get("tags", {}), "has_art": bool(meta.get("has_art", False)),
        "lyrics": meta.get("lyrics", ""), "lrc": meta.get("lrc", ""),
        "proposal": _public_proposal(proposal), "updated_at": row["updated_at"],
    }
    if detail:
        analysis = db.fetchone(settings.db_path,'SELECT * FROM analyses WHERE track_id=?',(row['id'],))
        result['analysis'] = {**json.loads(analysis['payload']), 'updated_at':analysis['updated_at'], 'stale':analysis['input_sha256']!=row['sha256']} if analysis else None
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
    result['retry_at']=json.loads(row['payload']).get('retry_at') if row['stage']=='waiting_provider' else None
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
    from . import automation,scheduling,exports
    automation.install(app,settings)
    scheduling.install(app,settings)
    exports.install(app,settings)
    @app.get('/api/openapi.json',include_in_schema=False)
    def schema(request:Request):
        security.require_session(request,settings)
        return app.openapi()
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
        count = {"total": len(rows), "missing_tags": 0, "missing_art": 0, "missing_lyrics": 0, "needs_review": 0, "published": 0, "cue_sheets": 0, "cue_tracks": 0}
        formats = {}
        for row in rows:
            meta = json.loads(row["metadata"])
            fmt = meta.get("format", "unknown")
            formats[fmt] = formats.get(fmt, 0) + 1
            if fmt == 'cue':
                count['cue_sheets'] += 1
                count['cue_tracks'] += len(meta.get('cue', {}).get('tracks', []))
                count['needs_review'] += bool(meta.get('cue', {}).get('errors')) or row['status'] == 'missing'
                continue
            count["missing_tags"] += not (meta.get("tags", {}).get("title") and meta.get("tags", {}).get("artist"))
            count["missing_art"] += not meta.get("has_art", False)
            count["missing_lyrics"] += not meta.get("lrc", "")
            count["needs_review"] += row["status"] in {"needs_review", "conflict", "error", "missing"}
            count["published"] += meta.get("publication", {}).get("sha256") == row["sha256"]
        active = db.fetchone(settings.db_path, "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','running','waiting','interrupted')")["n"]
        heartbeat = json.loads(db.setting(settings.db_path, "worker_heartbeat", "{}"))
        return {**count, "formats": formats, "active_jobs": active, "read_only": not settings.writable,
                "worker": {**heartbeat, "online": time.time()-heartbeat.get("at", 0) < 30 and heartbeat.get("state") != "stopped"},
                "capabilities": {"edit": True, "apply": settings.writable, "publish": settings.writable, "audit": True, "metadata_search": settings.external_metadata, "recognition": settings.external_fingerprints, "cue_export": settings.writable, "replaygain": True, "asr": bool(settings.inference_url)},
                "roots": {"work": str(settings.work_root)}, "version": __version__}

    @app.get("/api/tracks")
    def tracks(request: Request, search: str = Query("", max_length=200), page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200), filter: str = Query("all")):
        security.require_session(request, settings)
        clauses = ["t.is_current=1"]
        args = []
        if search:
            clauses.append("(casefold(t.relative_path) LIKE casefold(?) ESCAPE '\\' OR EXISTS (SELECT 1 FROM json_tree(t.metadata,'$.tags') v WHERE v.type='text' AND casefold(v.atom) LIKE casefold(?) ESCAPE '\\') OR EXISTS (SELECT 1 FROM json_tree(t.metadata,'$.cue.tracks') v WHERE v.type='text' AND casefold(v.atom) LIKE casefold(?) ESCAPE '\\'))")
            term = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [term, term, term]
        filters = {
            "all": "1=1",
            "cue": "json_extract(t.metadata,'$.format')='cue'",
            "review": "(t.status IN ('needs_review','conflict','error','missing') OR EXISTS (SELECT 1 FROM proposals p WHERE p.track_id=t.id AND p.input_sha256=t.sha256))",
            "missing_lyrics": "json_extract(t.metadata,'$.format')<>'cue' AND COALESCE(json_extract(t.metadata,'$.lrc'),'')=''",
            "missing_art": "json_extract(t.metadata,'$.format')<>'cue' AND COALESCE(json_extract(t.metadata,'$.has_art'),0)=0",
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
            row = conn.execute("SELECT sha256,metadata FROM tracks WHERE id=? AND is_current=1", (track_id,)).fetchone()
            if not row:
                raise security.error(404, "track_not_found", "Трек не найден")
            meta = json.loads(row['metadata'])
            if meta.get('format') == 'cue' or meta.get('cue_album_image'):
                raise security.error(409, 'cue_read_only', 'CUE и альбомные образы открыты только для чтения')
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
        row = _track_row(settings, track_id)
        if json.loads(row['metadata']).get('format') == 'cue':
            raise security.error(409, 'cue_select_track', 'Выберите композицию в списке CUE')
        path = resolve_track_path(settings, row)
        return FileResponse(path, media_type=intake.MIMES.get(json.loads(row["metadata"]).get("format")) or mimetypes.guess_type(path.name)[0] or "application/octet-stream", headers={"Cache-Control": "private, no-store"})

    @app.get('/api/tracks/{track_id}/cue/{number}/audio')
    def cue_audio(request: Request, track_id: str, number: int):
        security.require_session(request, settings)
        header = request.headers.get('range', '')
        if ',' in header or len(header) > 100:
            raise security.error(416, 'unsupported_range', 'Поддерживается один диапазон байтов')
        row = _track_row(settings, track_id)
        try:
            path = cue.playback_path(settings.work_root, row['relative_path'], json.loads(row['metadata']), number)
        except cue.MediaError as exc:
            raise security.error(409, exc.code, exc.message)
        except OSError:
            raise security.error(404, 'cue_file_missing', 'Файл CUE или аудио недоступен')
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream', headers={'Cache-Control': 'private, no-store'})

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
        automation.check_job(active,body.kind)
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 128:
            raise security.error(428, "idempotency_key_required", "Передайте Idempotency-Key длиной до 128 символов")
        if body.kind in {"apply", "rollback", "publish", "extract_cue", "normalize_audio"} and not settings.writable:
            raise security.error(409, "read_only", "Запись отключена до проверки конфигурации хранилища")
        if set(body.params) - {"proposal_id", "history_id", "allow_incomplete", "exception_reason", "scope", "language", "generation"}:
            raise security.error(422, "invalid_params", "Неизвестный параметр задания")
        if body.params.get("allow_incomplete") and (not isinstance(body.params.get("exception_reason"), str) or not 5 <= len(body.params["exception_reason"]) <= 1000):
            raise security.error(422, "exception_reason_required", "Укажите причину публикации неполных метаданных")
        if body.params.get('language') and not re.fullmatch('[a-z]{2,3}',str(body.params['language'])):
            raise security.error(422,'invalid_language','Язык: ru, en или автоматическое определение')
        if 'generation' in body.params and (type(body.params['generation']) is not int or not 0<=body.params['generation']<=1000000):
            raise security.error(422,'invalid_generation','Неверная версия запроса ASR')
        if body.kind in {'transcribe','align'} and not settings.inference_url:
            raise security.error(409,'inference_disabled','ASR не настроен')
        ids = list(dict.fromkeys(body.track_ids))
        if body.kind=='replaygain_album' and not 1<=len(ids)<=200:
            raise security.error(422,'invalid_album','Выберите 1-200 файлов одной группы альбома')
        if body.params.get('scope'):
            if body.kind not in {'analyze','recognize','replaygain'} or body.params['scope'] not in {'all','pending'} or ids:
                raise security.error(422,'invalid_scope','Полная библиотека доступна только для анализа')
            ids=[row['id'] for row in db.fetchall(settings.db_path,"SELECT id FROM tracks WHERE is_current=1 AND status NOT IN ('error','missing') AND json_extract(metadata,'$.format')<>'cue' AND COALESCE(json_extract(metadata,'$.cue_album_image'),0)=0 LIMIT 10000")]
            if body.params['scope']=='pending':
                pending=[]
                known={}
                for row in db.fetchall(settings.db_path,"SELECT a.track_id,json_extract(s.value,'$.provider') AS provider,json_extract(s.value,'$.state') AS state FROM analyses a JOIN tracks t ON t.id=a.track_id AND t.sha256=a.input_sha256 AND t.is_current=1,json_each(a.payload,'$.sources') s"):
                    known.setdefault(row['track_id'],{})[row['provider']]=row['state']
                for track_id in ids:
                    sources=known.get(track_id,{})
                    wanted=['shazam'] if body.kind=='recognize' else ['local_replaygain'] if body.kind=='replaygain' else ['musicbrainz','lrclib']
                    if any(sources.get(p) not in {'candidates','candidate','no_match','fingerprint_match'} for p in wanted):pending.append(track_id)
                ids=pending
                if not ids:raise security.error(409,'nothing_to_process','Все доступные записи уже обработаны этим этапом')
        if body.kind=='analyze' and not settings.external_metadata:
            raise security.error(409,'provider_disabled','Внешние метаданные отключены')
        if body.kind=='recognize' and not settings.external_fingerprints:
            raise security.error(409,'provider_disabled','Отправка аудиоотпечатков отключена')
        if body.kind in {"scan","export_ready"} and ids:
            raise security.error(422, "invalid_scope", "Сканирование выполняется для всей библиотеки")
        if body.kind not in {"scan","export_ready"} and not ids:
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
                meta = json.loads(track['metadata'])
                if body.kind=='normalize_audio' and (meta.get('write_supported') or meta.get('input_error')):
                    raise security.error(409,'native_or_invalid','Файл уже поддерживает запись либо поврежден')
                if body.kind=='extract_cue':
                    if meta.get('format')!='cue' or meta.get('cue',{}).get('errors'):
                        raise security.error(409,'invalid_cue','Нужен корректный CUE с доступными аудиофайлами')
                elif meta.get('format') == 'cue' or (meta.get('cue_album_image') and body.kind not in {'audit','analyze','transcribe','align'}):
                    raise security.error(409, 'cue_read_only', 'CUE и альбомные образы не изменяются заданиями записи')
                item = {"track_id": track_id, "sha256": track["sha256"], "relative_path": track["relative_path"]}
                if body.kind in {'transcribe','align'} and 'generation' not in body.params:
                    last=conn.execute("SELECT j.payload,j.state,i.value AS item,ji.state AS item_state FROM jobs j,json_each(j.payload,'$.items') i LEFT JOIN job_items ji ON ji.job_id=j.id AND ji.item_key=? WHERE j.kind=? AND json_extract(i.value,'$.track_id')=? AND json_extract(i.value,'$.sha256')=? ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1",(track_id,body.kind,track_id,track['sha256'])).fetchone()
                    generation=0
                    if last:
                        generation=json.loads(last['item']).get('generation',json.loads(last['payload']).get('params',{}).get('generation',0))
                        if last['item_state'] in {'failed','needs_review','interrupted'} or last['state'] in {'failed','cancelled','cancelled_partial','interrupted'}:generation+=1
                    if generation>1000000:raise security.error(409,'generation_limit','Достигнут предел повторов ASR')
                    item['generation']=generation
                if body.kind=='align':
                    proposal=conn.execute('SELECT payload FROM proposals WHERE track_id=? AND input_sha256=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(track_id,track['sha256'])).fetchone()
                    text=(json.loads(proposal['payload']).get('lrc') if proposal else '') or meta.get('lrc') or meta.get('lyrics')
                    if not text or len(text)>100000: raise security.error(422,'lyrics_required','Нужен текст длиной до 100000 символов')
                    item['reference_text']=text
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
            if active.get('api_token_id'):payload['auth_token_id']=active['api_token_id']
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,?,'queued','pending',?,?,?,?,?)", (identity,body.kind,db.encode(payload),len(items),now,now,active["auth_epoch"]))
            conn.execute("INSERT INTO idempotency VALUES('owner:jobs',?,?,?,?)", (key,request_hash,identity,now))
            db.event(conn, "job_created", {"job_id": identity, "kind": body.kind})
            row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone())
        return public_job(settings, row)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(request: Request, job_id: str):
        active=security.require_session(request, settings, True)
        with db.transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise security.error(404, "job_not_found", "Задание не найдено")
            automation.check_job(active,row['kind'])
            if row["state"] in {"queued", "waiting", "paused"}:
                conn.execute("UPDATE jobs SET state='cancelled',stage='cancelled',cancel_requested=1,updated_at=? WHERE id=?", (db.utcnow(),job_id))
            elif row["state"] == "running":
                conn.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (db.utcnow(),job_id))
            db.event(conn, "job_cancel_requested", {"job_id": job_id})
        return public_job(settings, db.fetchone(settings.db_path, "SELECT * FROM jobs WHERE id=?", (job_id,)))

    @app.get('/api/reports/collection')
    def collection_report(request: Request):
        security.require_session(request,settings)
        return reports.summary(settings)

    @app.get('/api/reports/export/{kind}')
    def export_report(request:Request,kind:Literal['json','csv']):
        security.require_session(request,settings)
        return Response(reports.export(settings,kind),media_type='application/json' if kind=='json' else 'text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="phonoteka-report.{kind}"','Cache-Control':'private, no-store'})

    @app.get('/api/providers')
    def provider_status(request: Request):
        security.require_session(request, settings)
        providers=[{'id':x,'configured':settings.external_metadata,'capabilities':caps} for x,caps in [('musicbrainz',['metadata']),('lrclib',['lyrics']),('coverartarchive',['cover'])]]
        providers += [{'id':'shazam','configured':settings.external_fingerprints,'capabilities':['fingerprint'],'unofficial':True},
                      {'id':'theaudiodb','configured':settings.external_metadata and settings.theaudiodb_enabled,'capabilities':['release']+(['cover'] if settings.theaudiodb_artwork else [])},
                      {'id':'discogs','configured':bool(settings.external_metadata and settings.discogs_enabled and settings.discogs_token_file and settings.discogs_token_file.is_file()),'capabilities':['release']},
                      *[{'id':x,'configured':False,'state':'key_required'} for x in ['acoustid','audd','acrcloud']]]
        for provider in providers:
            provider['calls_24h']=db.fetchone(settings.db_path,'SELECT COUNT(*) AS n FROM provider_calls WHERE provider=? AND at>?',(provider['id'],time.time()-86400))['n']
            breaker=json.loads(db.setting(settings.db_path,'provider_breaker:'+provider['id'],'{}'))
            provider['retry_at']=breaker.get('until') if breaker.get('until',0)>time.time() else None
            provider['state']='disabled' if not provider['configured'] else breaker.get('reason','ready') if provider['retry_at'] else 'ready'
            if provider['id'] in {'acoustid','audd','acrcloud'}:provider['state']='key_required'
        return {'external_metadata':settings.external_metadata,'external_fingerprints':settings.external_fingerprints,'daily_limit':settings.provider_daily_limit,'providers':providers}

    @app.post('/api/tracks/{track_id}/analysis/select',status_code=201)
    async def select_analysis(request: Request, track_id: str):
        security.require_session(request, settings, True)
        try: body = await request.json()
        except ValueError: raise security.error(422,'invalid_selection','Ожидается JSON')
        if not isinstance(body,dict): raise security.error(422,'invalid_selection','Ожидается объект')
        ids = body.get('candidate_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(not isinstance(x,str) for x in ids):
            raise security.error(422,'invalid_selection','Выберите от одного до восьми кандидатов')
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1',(track_id,)).fetchone()
            analysis = conn.execute('SELECT * FROM analyses WHERE track_id=?',(track_id,)).fetchone()
            if not row or not analysis: raise security.error(404,'analysis_missing','Сначала выполните поиск')
            if request.headers.get('if-match','').strip('"')!=row['sha256'] or analysis['input_sha256']!=row['sha256']:
                raise security.error(412,'revision_conflict','Файл изменился; повторите анализ')
            meta=json.loads(row['metadata'])
            if meta.get('format')=='cue' or meta.get('cue_album_image') or meta.get('input_error'):
                raise security.error(409,'read_only','Запись в этот файл недоступна')
            candidates=json.loads(analysis['payload']).get('candidates',[])
            chosen=[x for x in candidates if x['id'] in ids]
            if len(chosen)!=len(set(ids)): raise security.error(422,'candidate_missing','Кандидат устарел')
            if len({x['kind'] for x in chosen})!=len(chosen): raise security.error(422,'candidate_conflict','Выберите один вариант каждого типа')
            payload={'tags':{},'sources':[]}
            for candidate in chosen:
                payload['tags'].update(candidate.get('tags',{}))
                if candidate.get('lrc') or candidate.get('plain_lyrics'): payload['lrc']=candidate.get('lrc') or candidate['plain_lyrics']
                if candidate.get('cover'): payload['cover']=candidate['cover']
                payload['sources'].append({k:candidate[k] for k in ('id','provider','source_url','source_path','identification','timing_verified') if k in candidate})
            identity=str(uuid.uuid4());now=db.utcnow()
            conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',(identity,track_id,row['sha256'],db.encode(payload),now))
            db.event(conn,'proposal_created',{'track_id':track_id,'proposal_id':identity})
        return {'id':identity,'track_id':track_id,'input_sha256':row['sha256']}

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
