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

from . import __version__, db, security, cue, reports, intake, youtube, job_activity, field_policy, media, provider_policy, comparison, lyrics_review
from . import lyrics_document
from . import media_slots
from . import artwork
from .lyrics_document import DocumentEdit as LyricsDocumentEdit
from .telemetry import log
from . import webhooks,provider_attempts
from .config import Settings
from . import publication
from . import retention, export_leases
from . import album_gain_policy, api_facades, audit_status, recording_review, input_readiness, automation


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
    lyrics_document: LyricsDocumentEdit | None = None
    slot_selection: media_slots.SlotSelection | None = None
    artwork_preview_id: str | None = Field(default=None, pattern='^[a-f0-9-]{36}$')
    confirm_smaller_cover: bool = False


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["scan", "audit", "apply", "rollback", "publish", "rollback_ready", "publish_sidecars", "analyze", "recognize", "transcribe", "align", "extract_cue", "replaygain", "replaygain_album", "export_ready", "normalize_audio", "youtube", "release_audit"]
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
            limit = 29 * 1024 * 1024 if scope.get('path', '').endswith('/artwork-preview') else self.max_bytes
            if received > limit:
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


def public_track(settings: Settings, row: dict, detail=False, *, proposal=None, load_proposal=True):
    meta = json.loads(row["metadata"])
    if load_proposal:
        proposal = db.fetchone(settings.db_path, "SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["id"],))
    result = {
        **meta, "id": row["id"], "relative_path": row["relative_path"],
        "filename": Path(row["relative_path"]).name,
        "sha256": row["sha256"], "revision": row["sha256"], "status": row["status"],
        "hash_verified_at": meta.get('hash_verified_at'),
        "can_write": settings.writable and not meta.get('cue_album_image') and not recording_review.possible_mix(meta,row['sha256']) and bool(meta.get("write_supported", meta.get("format", "").lower() in {"mp3", "flac"})) and row["status"] != "missing",
        "tags": {key: (value[0] if value else "") if isinstance(value, list) else value for key, value in meta.get("tags", {}).items()},
        "tag_values": meta.get("tags", {}), "has_art": bool(meta.get("has_art", False)),
        "lyrics": meta.get("lyrics", ""), "lrc": meta.get("lrc", ""),
        "proposal": _public_proposal(proposal), "updated_at": row["updated_at"],
        **audit_status.describe(meta, row['sha256'], row['status']),
        'input_readiness': {**(meta['input_readiness'] if isinstance(meta.get('input_readiness'),dict) else {}),
            **input_readiness.describe(meta,grace_seconds=settings.input_grace_seconds)},
    }
    if detail:
        result['lyrics_document'] = lyrics_document.public_document(settings, row)
        result['artwork_transform'] = meta.get('artwork_transform')
        analysis = db.fetchone(settings.db_path,'SELECT * FROM analyses WHERE track_id=?',(row['id'],))
        result['analysis'] = {**provider_policy.public_analysis(settings,json.loads(analysis['payload'])), 'updated_at':analysis['updated_at'], 'stale':analysis['input_sha256']!=row['sha256']} if analysis else None
        result['analysis'] = lyrics_document.analysis_view(row, result['analysis'], result['lyrics_document'])
        rows = db.fetchall(settings.db_path, "SELECT * FROM history WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (row["id"],))
        result["history"] = [public_history(settings, item) for item in rows]
    return result


def public_history(settings, row):
    # Retention marks pruned versions; missing backups are never advertised.
    details = json.loads(row["details"])
    backup = Path(details['backup_path']) if details.get('backup_path') else None
    retained = False
    if backup:
        try:
            media._no_links(backup)
            retained = backup.is_absolute() and '..' not in backup.parts and backup.is_relative_to(settings.state_dir.absolute()/'backups'/row['track_id']) and backup.is_file() and backup.stat().st_nlink == 1
        except (OSError, media.MediaError):
            pass
    if details.get('backup_pruned') or not retained:
        details['backup_path'] = None
    # Paths of backups/journals are internal worker handles, never user input.
    safe = {key: val for key, val in details.items() if key not in {"backup_path", "journal_path", "target", "source", "destination"}}
    return {key: row[key] for key in ("id", "track_id", "job_id", "kind", "before_sha256", "after_sha256", "created_at")} | {
        "details": safe, "rollback_available": bool(details.get("backup_path")) and row["kind"] in {"apply", "rollback"}
    }


def public_job(settings: Settings, row: dict, detail=False, activity=None):
    total = row["total"]
    result = {key: row[key] for key in ("id", "kind", "state", "stage", "total", "completed", "failed", "cancel_requested", "created_at", "updated_at", "error")}
    result["progress"] = round(100 * (row["completed"] + row["failed"]) / total) if total else 0
    result["cancel_requested"] = bool(row["cancel_requested"])
    payload=json.loads(row['payload'])
    result['retry_at']=payload.get('retry_at') if row['stage'] in {'waiting_provider','waiting_storage'} else None
    result['pause_requested']=bool(payload.get('pause_requested'))
    result['authorization']=automation.public_authorization(row,payload)
    result.update(activity if activity is not None else job_activity.summaries(settings, [row])[row['id']])
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
        await asyncio.to_thread(db.initialize, settings.db_path, settings=settings)
        await asyncio.to_thread(security.bootstrap, settings)
        await asyncio.to_thread(webhooks.configure,settings)
        stop=asyncio.Event()
        notifier=asyncio.create_task(webhooks.Sender(settings).run(stop))
        log('service_ready')
        try:yield
        finally:
            stop.set()
            try:await asyncio.wait_for(notifier,timeout=12)
            except asyncio.TimeoutError:notifier.cancel()
            log('service_stopping')

    app = FastAPI(title="Фонотека API", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    from . import automation,scheduling,exports,operations,observability
    observability.install(app,settings)
    webhooks.install(app,settings)
    provider_attempts.install(app,settings)
    automation.install(app,settings)
    scheduling.install(app,settings)
    exports.install(app,settings)
    comparison.install(app,settings)
    provider_policy.install(app,settings)
    operations.install(app,settings)
    youtube.install(app,settings)
    field_policy.install(app,settings)
    from . import release_review
    release_review.install(app,settings)
    from . import album_batch
    album_batch.install(app,settings)
    lyrics_review.install(app,settings)
    lyrics_document.install(app,settings)
    media_slots.install(app,settings)
    artwork.install(app,settings)
    publication.install(app,settings)
    album_gain_policy.install(app,settings)
    from . import publication_sidecars
    publication_sidecars.install(app,settings)
    from . import automatic_quality
    automatic_quality.install(app,settings)
    recording_review.install(app,settings)
    retention.install(app,settings)
    export_leases.install(app,settings)
    @app.get('/api/openapi.json',include_in_schema=False)
    def schema(request:Request):
        security.require_session(request,settings)
        return app.openapi()
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.app_url).hostname, "localhost", "127.0.0.1", "testserver", "app", "phonoteka-app"])

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
        return security.public_session(settings, active)

    @app.post('/api/auth/activity')
    def auth_activity(request: Request, body: dict):
        if body:
            raise security.error(422, 'validation_error', 'User activity accepts an empty object')
        return security.public_session(settings, security.record_activity(request, settings))

    @app.post("/api/auth/login")
    def auth_login(request: Request, response: Response, body: LoginInput):
        token, csrf = security.login(request, settings, body.username, body.password)
        active = security.session_by_hash(settings, security.token_hash(token))
        if not active:
            raise security.error(401, 'authentication_required', 'Session changed; sign in again')
        response.set_cookie(security.cookie_name(settings), token, httponly=True, secure=settings.secure_cookie,
                            samesite="strict", path="/", max_age=settings.session_absolute_seconds)
        return security.public_session(settings, active)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        active = security.require_session(request, settings, True)
        with db.transaction(settings.db_path) as conn:
            security.revoke_sessions(conn, active['token_hash'])
        response.delete_cookie(security.cookie_name(settings), path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return {"authenticated": False, "csrf_token": None, "username": None}

    @app.post("/api/auth/logout-all")
    def auth_logout_all(request: Request, response: Response):
        security.require_session(request, settings, True)
        with db.transaction(settings.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            security.revoke_sessions(conn)
            db.event(conn, "owner_sessions_revoked", payload={"sessions": count})
        response.delete_cookie(security.cookie_name(settings), path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return {"authenticated": False, "csrf_token": None, "username": None}

    @app.get("/api/summary")
    def summary(request: Request):
        security.require_session(request, settings)
        # Aggregate in SQLite rather than decoding all 10k documents under the
        # Python GIL on every four-second status poll.
        count = db.fetchone(settings.db_path, """WITH catalog AS (
            SELECT *,COALESCE(json_extract(metadata,'$.format'),'unknown') AS fmt FROM tracks WHERE is_current=1
        ) SELECT COUNT(*) AS total,
          COALESCE(SUM(fmt='cue'),0) AS cue_sheets,
          COALESCE(SUM(CASE WHEN fmt='cue' THEN COALESCE(json_array_length(metadata,'$.cue.tracks'),0) ELSE 0 END),0) AS cue_tracks,
          COALESCE(SUM(CASE WHEN fmt='cue' THEN COALESCE(json_array_length(metadata,'$.cue.errors'),0)>0 OR status='missing'
            ELSE status IN ('needs_review','conflict','error','missing') END),0) AS needs_review,
          COALESCE(SUM(fmt<>'cue' AND (COALESCE(json_extract(metadata,'$.tags.title'),'') IN ('','[]') OR COALESCE(json_extract(metadata,'$.tags.artist'),'') IN ('','[]'))),0) AS missing_tags,
          COALESCE(SUM(fmt<>'cue' AND NOT COALESCE(json_extract(metadata,'$.has_art'),0)),0) AS missing_art,
          COALESCE(SUM(fmt<>'cue' AND COALESCE(json_extract(metadata,'$.lrc'),'')=''),0) AS missing_lyrics,
          COALESCE(SUM(fmt<>'cue' AND COALESCE(json_extract(metadata,'$.publication.sha256')=sha256,0)),0) AS published
          FROM catalog""")
        formats = {row['format']:row['n'] for row in db.fetchall(settings.db_path,"SELECT COALESCE(json_extract(metadata,'$.format'),'unknown') AS format,COUNT(*) AS n FROM tracks WHERE is_current=1 GROUP BY format")}
        active = db.fetchone(settings.db_path, "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','running','paused','waiting_authorization','waiting','waiting_provider','deferred','interrupted')")["n"]
        heartbeat = json.loads(db.setting(settings.db_path, "worker_heartbeat", "{}"))
        recovery=db.setting(settings.db_path,'recovery_required','false')=='true';writable=settings.writable and not recovery
        return {**count, "formats": formats, "active_jobs": active, "read_only": not writable,'recovery_required':recovery,
                "worker": {**heartbeat, "online": time.time()-heartbeat.get("at", 0) < 30 and heartbeat.get("state") != "stopped"},
                "capabilities": {"edit": True, "apply": writable, "publish": writable, "audit": True, "youtube": settings.youtube_enabled and not recovery, "metadata_search": settings.external_metadata and not recovery, "recognition": settings.external_fingerprints and not recovery, "cue_export": writable, "replaygain": not recovery, "asr": bool(settings.inference_url) and not recovery},
                "roots": {"work": str(settings.work_root)}, "version": __version__}

    def track_selection(search, filter):
        clauses = ["t.is_current=1"]
        args = []
        if search:
            clauses.append("t.id IN (SELECT track_id FROM tracks_search WHERE document LIKE ? ESCAPE '\\')")
            term = "%" + search.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.append(term)
        filters = {
            "all": "1=1",
            "cue": "json_extract(t.metadata,'$.format')='cue'",
            "review": "(t.status IN ('needs_review','conflict','error','missing') OR EXISTS (SELECT 1 FROM proposals p WHERE p.track_id=t.id AND p.input_sha256=t.sha256))",
            "missing_lyrics": "json_extract(t.metadata,'$.format')<>'cue' AND COALESCE(json_extract(t.metadata,'$.lrc'),'')=''",
            "missing_art": "json_extract(t.metadata,'$.format')<>'cue' AND COALESCE(json_extract(t.metadata,'$.has_art'),0)=0",
            "published": "json_extract(t.metadata,'$.publication.sha256')=t.sha256",
        }
        filters.update({name: f"phonoteka_audit_filter(t.metadata,t.sha256,t.status,'{name}')=1"
                        for name in audit_status.FILTERS})
        if filter not in filters:
            raise security.error(422, "invalid_filter", "Неизвестный фильтр")
        clauses.append(filters[filter])
        return " AND ".join(clauses), args

    @app.get("/api/tracks")
    def tracks(request: Request, search: str = Query("", max_length=200), page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200), filter: str = Query("all")):
        security.require_session(request, settings)
        where, args = track_selection(search, filter)
        with db.connect(settings.db_path) as conn:
            conn.execute('BEGIN')
            total = conn.execute(f"SELECT COUNT(*) AS n FROM tracks t WHERE {where}", args).fetchone()["n"]
            rows = conn.execute(f"SELECT t.* FROM tracks t WHERE {where} ORDER BY t.relative_path,t.id LIMIT ? OFFSET ?", (*args, page_size, (page-1)*page_size)).fetchall()
            ids=[row['id'] for row in rows]
            proposals={row['track_id']:row for row in conn.execute(
                "SELECT p.* FROM proposals p WHERE p.track_id IN (%s) AND p.id=(SELECT latest.id FROM proposals latest WHERE latest.track_id=p.track_id ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1)" % ','.join('?' for _ in ids),ids)} if ids else {}
        return {"items": [public_track(settings, row, proposal=proposals.get(row['id']),load_proposal=False) for row in rows], "total": total, "page": page, "page_size": page_size}

    @app.get("/api/tracks/{track_id}/neighbors")
    def track_neighbors(request: Request, track_id: str, search: str = Query("", max_length=200), filter: str = Query("all")):
        security.require_session(request, settings)
        _track_row(settings, track_id)
        where, args = track_selection(search, filter)
        # The same stable catalog ordering, across every page. Broken/missing files and
        # CUE documents are not playable neighbors; CUE children are navigated in UI.
        row = db.fetchone(settings.db_path, f"""WITH catalog AS (
            SELECT t.*, ROW_NUMBER() OVER (ORDER BY t.relative_path,t.id) AS catalog_position
            FROM tracks t WHERE {where}
        ), ordered AS (
            SELECT t.id, t.catalog_position, ROW_NUMBER() OVER w AS position, COUNT(*) OVER () AS total,
                LAG(t.id) OVER w AS previous_id, LEAD(t.id) OVER w AS next_id
            FROM catalog t WHERE json_extract(t.metadata,'$.format')<>'cue'
                AND json_extract(t.metadata,'$.input_error') IS NULL AND t.status<>'missing'
            WINDOW w AS (ORDER BY t.relative_path,t.id)
        ) SELECT * FROM ordered WHERE id=?""", (*args, track_id))
        return dict(row) if row else {"id": track_id, "position": None, "catalog_position": None, "total": 0, "previous_id": None, "next_id": None}

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
        if set(body.tags) - (set(field_policy.FIELDS) - {'cover', 'lrc'}):
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
            try:
                mime, _, _ = media._cover(data)
                if mime != body.cover.split(';', 1)[0][5:]: raise media.MediaError('invalid_cover', 'MIME обложки не совпадает с содержимым')
            except media.MediaError as exc: raise security.error(422, exc.code, exc.message) from exc
        now, identity = db.utcnow(), str(uuid.uuid4())
        payload = body.model_dump(exclude_unset=True, exclude={"base_proposal_id", "lyrics_document", "artwork_preview_id", "confirm_smaller_cover"})
        with db.transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM tracks WHERE id=? AND is_current=1", (track_id,)).fetchone()
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
            payload = media_slots.validate_proposal(settings, row, payload)
            if 'cover' in body.model_fields_set and not body.artwork_preview_id:
                payload.pop('artwork_transform', None)
            payload = artwork.proposal(conn, row, payload, body.artwork_preview_id, body.confirm_smaller_cover)
            if 'lrc' in body.model_fields_set and body.lyrics_document is None:
                payload.pop('lyrics_document', None)
            try:
                payload = lyrics_document.propose(conn, row, payload, body.lyrics_document)
            except media.MediaError as exc:
                raise security.error(412 if exc.code == 'lyrics_document_conflict' else 409, exc.code, exc.message) from exc
            payload = field_policy.require_payload(settings, conn, row, payload)
            payload = provider_policy.api_payload(conn,row,payload,'cache')
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
        row = _track_row(settings, track_id)
        try:
            content = read_art(resolve_track_path(settings, row), {"work": settings.work_root, "source": settings.source_root}, slot=json.loads(row['metadata']).get('preferred_cover'))
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc
        if not content:
            raise security.error(404, "art_not_found", "Обложка отсутствует")
        data, mime = content
        return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store"})

    @app.get("/api/jobs")
    def jobs(request: Request, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
        security.require_session(request, settings)
        rows = db.fetchall(settings.db_path, "SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (page_size,(page-1)*page_size))
        total = db.fetchone(settings.db_path, "SELECT COUNT(*) AS n FROM jobs")["n"]
        activity = job_activity.summaries(settings, rows)
        return {"items": [public_job(settings, row, activity=activity[row['id']]) for row in rows], "total": total, "page": page, "page_size": page_size}

    @app.get("/api/jobs/{job_id}")
    def get_job(request: Request, job_id: str):
        security.require_session(request, settings)
        row = db.fetchone(settings.db_path, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise security.error(404, "job_not_found", "Задание не найдено")
        return public_job(settings, row, True)

    @app.get("/api/jobs/{job_id}/activity")
    def get_job_activity(request: Request, job_id: str, limit: int = Query(80, ge=1, le=200), after: int = Query(0, ge=0)):
        security.require_session(request, settings)
        row = db.fetchone(settings.db_path, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise security.error(404, "job_not_found", "Задание не найдено")
        activity = job_activity.snapshot(settings, row, limit, after)
        return {"job": public_job(settings, row, activity=activity.pop("summary")), **activity}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: str, after: int = Query(0, ge=0)):
        await asyncio.to_thread(security.require_session, request, settings)
        if not await asyncio.to_thread(db.fetchone, settings.db_path, "SELECT id FROM jobs WHERE id=?", (job_id,)):
            raise security.error(404, "job_not_found", "Задание не найдено")
        # Native EventSource sends Last-Event-ID on reconnect. The query cursor
        # permits an initial snapshot + stream without replaying the entire log.
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            if cursor < 0:
                raise ValueError
        except ValueError:
            raise security.error(422, "invalid_event_id", "Неверный идентификатор события")
        async def stream():
            nonlocal cursor
            heartbeat = time.monotonic()
            yield "retry: 2000\n: connected\n\n"
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(security.require_session, request, settings)
                except HTTPException:
                    break
                row = await asyncio.to_thread(db.fetchone, settings.db_path, "SELECT * FROM jobs WHERE id=?", (job_id,))
                if not row:
                    break
                activity = await asyncio.to_thread(job_activity.snapshot, settings, row, 100, cursor)
                if activity["resync_required"]:
                    yield f"id: {activity['cursor']}\nevent: resync_required\ndata: {{}}\n\n"
                for event in activity["events"]:
                    yield f"id: {event['id']}\nevent: job_activity\ndata: {db.encode(event)}\n\n"
                cursor = activity["cursor"]
                if not activity["events"] and time.monotonic() - heartbeat >= 15:
                    yield f"id: {cursor}\n: keepalive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.1 if activity["has_more"] else 2)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "private, no-store"})

    @app.post("/api/jobs", status_code=202)
    def create_job(request: Request, body: JobInput):
        active = security.require_session(request, settings, True, owner_only=body.kind in {'rollback_ready', 'publish_sidecars'})
        automation.check_job(active,body.kind)
        if db.setting(settings.db_path,'recovery_required','false')=='true' and body.kind not in {'scan','audit'}:
            raise security.error(409,'recovery_required','После восстановления доступны только сканирование и аудит до сверки администратором')
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 128:
            raise security.error(428, "idempotency_key_required", "Передайте Idempotency-Key длиной до 128 символов")
        if body.kind in {"apply", "rollback", "publish", "rollback_ready", "publish_sidecars", "extract_cue", "normalize_audio"} and not settings.writable:
            raise security.error(409, "read_only", "Запись отключена до проверки конфигурации хранилища")
        if set(body.params) - {"proposal_id", "history_id", "expected_ready_sha256", "allow_incomplete", "exception_reason", "scope", "language", "generation", "youtube_lookup", "youtube_search", "release_id", "plan_id", "acceptance_mode", "release_review_id", "release_review_version", "publication_plan_id"}:
            raise security.error(422, "invalid_params", "Неизвестный параметр задания")
        if 'publication_plan_id' in body.params and (body.kind != 'publish' or not re.fullmatch('[a-f0-9-]{36}', str(body.params['publication_plan_id']))):
            raise security.error(422, 'invalid_params', 'План публикации относится только к publish')
        if {'release_review_id','release_review_version'} & body.params.keys():
            if body.kind != 'replaygain_album' or not re.fullmatch('[a-f0-9-]{36}', str(body.params.get('release_review_id',''))) or type(body.params.get('release_review_version')) is not int or body.params['release_review_version'] < 1:
                raise security.error(422, 'invalid_params', 'Укажите точную версию принятого состава релиза для Album Gain')
        if 'acceptance_mode' in body.params and (body.kind != 'apply' or not isinstance(body.params['acceptance_mode'], str) or body.params['acceptance_mode'] not in {'manual', 'automatic'}):
            raise security.error(422, 'invalid_acceptance_mode', 'Режим manual/automatic применяется только к записи предложения')
        if body.kind == 'publish_sidecars':
            if set(body.params) != {'plan_id'} or not isinstance(body.params['plan_id'], str) or not re.fullmatch('[a-f0-9-]{36}', body.params['plan_id']):
                raise security.error(422, 'sidecar_plan_required', 'Выберите сохраненный план sidecar')
        elif 'plan_id' in body.params:
            raise security.error(422, 'invalid_params', 'План sidecar относится только к publish_sidecars')
        if body.kind == 'rollback_ready':
            if len(body.track_ids) != 1 or not isinstance(body.params.get('history_id'), str) or not 1 <= len(body.params['history_id']) <= 36 or not re.fullmatch('[a-f0-9]{64}', str(body.params.get('expected_ready_sha256', ''))):
                raise security.error(422, 'ready_revision_required', 'Выберите одну версию Ready и её ожидаемый SHA-256')
        elif 'expected_ready_sha256' in body.params:
            raise security.error(422, 'invalid_params', 'Ревизия Ready относится только к отдельному откату публикации')
        for name in ('youtube_lookup','youtube_search'):
            if name in body.params and (body.kind!='youtube' or type(body.params[name]) is not bool):
                raise security.error(422,'invalid_params','Параметры YouTube должны быть логическими значениями задания YouTube')
        if body.params.get('youtube_search') and not body.params.get('youtube_lookup'):
            raise security.error(422,'invalid_params','Поиск YouTube требует включенного сетевого запроса')
        if body.kind=='youtube' and body.params.get('youtube_lookup') and not settings.youtube_enabled:
            raise security.error(409,'provider_disabled','Сетевые запросы YouTube отключены')
        if body.params.get("allow_incomplete") and (not isinstance(body.params.get("exception_reason"), str) or not 5 <= len(body.params["exception_reason"]) <= 1000):
            raise security.error(422, "exception_reason_required", "Укажите причину публикации неполных метаданных")
        if body.params.get('language') and not re.fullmatch('[a-z]{2,3}',str(body.params['language'])):
            raise security.error(422,'invalid_language','Язык: ru, en или автоматическое определение')
        if 'generation' in body.params and (type(body.params['generation']) is not int or not 0<=body.params['generation']<=1000000):
            raise security.error(422,'invalid_generation','Неверная версия запроса ASR')
        if body.kind in {'transcribe','align'} and not settings.inference_url:
            raise security.error(409,'inference_disabled','ASR не настроен')
        ids = list(dict.fromkeys(body.track_ids))
        if 'release_id' in body.params and body.kind != 'release_audit':
            raise security.error(422, 'invalid_params', 'Выбор релиза относится к заданию сверки альбома')
        if body.kind == 'release_audit':
            if not settings.external_metadata:
                raise security.error(409, 'provider_disabled', 'Внешние метаданные отключены; комплектность релиза неизвестна')
            try:
                release_review.validate_request(body.params, ids)
            except media.MediaError as exc:
                raise security.error(422, exc.code, exc.message) from exc
        if body.kind=='replaygain_album' and not 1<=len(ids)<=200:
            raise security.error(422,'invalid_album','Выберите 1-200 файлов одной группы альбома')
        if body.params.get('scope'):
            if body.kind not in {'analyze','recognize','replaygain','youtube'} or body.params['scope'] not in {'all','pending'} or ids:
                raise security.error(422,'invalid_scope','Полная библиотека доступна только для анализа')
            ids=[row['id'] for row in db.fetchall(settings.db_path,"SELECT id FROM tracks WHERE is_current=1 AND status NOT IN ('error','missing') AND json_extract(metadata,'$.format')<>'cue' AND COALESCE(json_extract(metadata,'$.cue_album_image'),0)=0 LIMIT 10000")]
            if body.params['scope']=='pending':
                pending=[]
                known={}
                for row in db.fetchall(settings.db_path,"SELECT a.track_id,json_extract(s.value,'$.provider') AS provider,json_extract(s.value,'$.state') AS state FROM analyses a JOIN tracks t ON t.id=a.track_id AND t.sha256=a.input_sha256 AND t.is_current=1,json_each(a.payload,'$.sources') s"):
                    known.setdefault(row['track_id'],{})[row['provider']]=row['state']
                for track_id in ids:
                    if body.kind=='youtube':
                        m=json.loads(db.fetchone(settings.db_path,'SELECT metadata FROM tracks WHERE id=?',(track_id,))['metadata']).get('youtube',{})
                        complete=m.get('lookup_complete') if body.params.get('youtube_lookup') else m.get('scanned_at')
                        if body.params.get('youtube_search'):complete=complete and (m.get('linked_id') or m.get('search_state') in {'candidates','no_match','insufficient_query'} or m.get('candidates'))
                        if not complete:pending.append(track_id)
                        continue
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
        scope = 'token:' + active['api_token_id'] + ':jobs' if active.get('api_token_id') else 'owner:jobs'
        now, identity = db.utcnow(), str(uuid.uuid4())
        with db.transaction(settings.db_path) as conn:
            previous = conn.execute("SELECT * FROM idempotency WHERE scope=? AND key=?", (scope,key)).fetchone()
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
                if body.kind in {'apply','publish','publish_sidecars','normalize_audio'} and recording_review.possible_mix(meta,track['sha256']):
                    raise security.error(409,'possible_mix_requires_manual_review','Составная запись не получает теги отдельной песни; подтвердите тип записи вручную')
                if body.kind=='normalize_audio' and (meta.get('write_supported') or meta.get('input_error')):
                    raise security.error(409,'native_or_invalid','Файл уже поддерживает запись либо поврежден')
                if body.kind=='extract_cue':
                    if meta.get('format')!='cue' or meta.get('cue',{}).get('errors'):
                        raise security.error(409,'invalid_cue','Нужен корректный CUE с доступными аудиофайлами')
                elif meta.get('format') == 'cue' or (meta.get('cue_album_image') and body.kind not in {'audit','analyze','transcribe','align','youtube'}):
                    raise security.error(409, 'cue_read_only', 'CUE и альбомные образы не изменяются заданиями записи')
                item = {"track_id": track_id, "sha256": track["sha256"], "relative_path": track["relative_path"]}
                if body.kind == 'publish':
                    item['publication_review'] = lyrics_review.freeze_publication(settings, track, conn=conn)
                    item['publication_target'] = publication.freeze_target(track)
                    if body.params.get('publication_plan_id'):
                        try:
                            api_facades.bind_plan(conn, body.params['publication_plan_id'], ids, item)
                        except media.MediaError as exc:
                            raise security.error(409, exc.code, exc.message) from exc
                if body.kind == 'rollback_ready':
                    try:
                        item.update(publication.rollback_selection(conn, track, body.params['history_id'], body.params['expected_ready_sha256']))
                    except media.MediaError as exc:
                        raise security.error(409, exc.code, exc.message) from exc
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
                    document=lyrics_document.effective(conn,dict(track))
                    item['reference_text'],item['lyrics_reference']=lyrics_document.alignment_reference(document,text)
                    reference_payload=json.loads(proposal['payload']) if proposal and json.loads(proposal['payload']).get('lrc') else {'lrc':text,'field_sources':meta.get('usage_provenance',{})}
                    reference_payload=provider_policy.api_payload(conn,track,reference_payload,'derived_analysis')
                    item['reference_sources']=reference_payload.get('field_sources',{})
                if body.kind == "apply":
                    if body.params.get("proposal_id"):
                        prop = conn.execute("SELECT * FROM proposals WHERE id=? AND track_id=?", (body.params["proposal_id"],track_id)).fetchone()
                    else:
                        prop = conn.execute("SELECT * FROM proposals WHERE track_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (track_id,)).fetchone()
                    if not prop or prop["input_sha256"] != track["sha256"]:
                        raise security.error(409, "proposal_stale", "Создайте актуальное предложение", file_id=track_id)
                    item["proposal_id"] = prop["id"]
                    item["proposal"] = json.loads(prop["payload"])
                    field_policy.require_payload(settings, conn, track, item['proposal'], 'apply')
                    item['proposal']=provider_policy.api_payload(conn,track,item['proposal'],'embed')
                    try:
                        album_gain_policy.payload(settings, conn, track, item['proposal'])
                    except media.MediaError as exc:
                        raise security.error(409, exc.code, exc.message) from exc
                    item['acceptance_mode'] = body.params.get('acceptance_mode', 'manual')
                    if item['acceptance_mode'] == 'automatic':
                        try:
                            item['automatic_quality'] = automatic_quality.freeze(settings, conn, track, prop, active, applied_payload=item['proposal'])
                        except media.MediaError as exc:
                            raise security.error(409, exc.code, exc.message, file_id=track_id) from exc
                if body.kind == "rollback":
                    if body.params.get("history_id"):
                        history = conn.execute("SELECT * FROM history WHERE id=? AND track_id=?", (body.params["history_id"],track_id)).fetchone()
                    else:
                        history = conn.execute("SELECT * FROM history WHERE track_id=? AND kind='apply' ORDER BY created_at DESC,rowid DESC LIMIT 1", (track_id,)).fetchone()
                    if not history or history["after_sha256"] != track["sha256"] or not json.loads(history["details"]).get("backup_path"):
                        raise security.error(409, "rollback_conflict", "Нет доступной резервной версии для текущего файла", file_id=track_id)
                    item["history_id"] = history["id"]
                items.append(item)
            if body.kind == 'publish_sidecars':
                try:
                    publication_sidecars.validate_enqueue(conn, body.params['plan_id'], ids, identity)
                except media.MediaError as exc:
                    raise security.error(409, exc.code, exc.message) from exc
            payload = {"items": items, "params": body.params}
            if active.get('api_token_id'):
                payload['auth_token_id']=active['api_token_id']
                payload['auth_scopes']=sorted(active['scopes'])
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,?,'queued','pending',?,?,?,?,?)", (identity,body.kind,db.encode(payload),len(items),now,now,active["auth_epoch"]))
            conn.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)", (scope,key,request_hash,identity,now))
            db.event(conn, "job_created", {"job_id": identity, "kind": body.kind})
            row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone())
        return public_job(settings, row)

    @app.post('/api/jobs/{job_id}/retry-storage')
    def retry_storage(request:Request,job_id:str):
        active = security.require_session(request,settings,True)
        with db.transaction(settings.db_path) as conn:
            row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row:raise security.error(404,'job_missing','Задача не найдена')
            automation.check_job(active,row['kind'])
            if row['state']!='queued' or row['stage']!='waiting_storage' or row['cancel_requested']:
                raise security.error(409,'job_not_waiting_storage','Задача не ожидает свободного места')
            control=conn.execute(f'SELECT {automation.CONTROL_COLUMNS} FROM jobs WHERE id=?',(job_id,)).fetchone()
            if automation.grant_reason(conn,control):
                raise security.error(409,'authorization_required','Сначала владелец должен подтвердить новые права задания')
            payload=json.loads(row['payload']);payload['retry_at']=0
            conn.execute("UPDATE jobs SET stage='pending',payload=?,updated_at=? WHERE id=?",(db.encode(payload),db.utcnow(),job_id))
            db.event(conn,'job_resume',{'job_id':job_id,'reason':'storage_recheck'})
        return public_job(settings,db.fetchone(settings.db_path,'SELECT * FROM jobs WHERE id=?',(job_id,)))

    @app.post('/api/jobs/{job_id}/pause')
    @app.post('/api/jobs/{job_id}/resume')
    def pause_resume(request:Request,job_id:str):
        action=request.url.path.rsplit('/',1)[-1]
        active=security.require_session(request,settings,True)
        denied=False
        with db.transaction(settings.db_path) as conn:
            row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row:raise security.error(404,'job_not_found','Задание не найдено')
            automation.check_job(active,row['kind'])
            if row['kind'] in {'scan','export_ready','replaygain_album','release_audit','publish_sidecars'}:raise security.error(409,'pause_unsupported','Этот этап выполняется одной согласованной операцией')
            payload=json.loads(row['payload'])
            if action=='pause':
                if row['state'] not in {'queued','running','paused','waiting_authorization'}:raise security.error(409,'job_not_active','Задание уже завершено')
                payload['pause_requested']=True
                state='paused' if row['state'] in {'queued','paused'} else row['state']
                stage='paused' if state=='paused' else row['stage']
            else:
                if row['state']=='waiting_authorization':raise security.error(409,'authorization_required','Ожидается явное подтверждение прав владельцем')
                if row['state']!='paused':raise security.error(409,'job_not_paused','Задание не приостановлено')
                if db.setting(settings.db_path,'recovery_required','false')=='true':raise security.error(409,'recovery_required','Сначала завершите восстановление')
                control=conn.execute(f'SELECT {automation.CONTROL_COLUMNS} FROM jobs WHERE id=?',(job_id,)).fetchone()
                reason=automation.grant_reason(conn,control)
                if reason:
                    automation.wait_for_authorization(conn,job_id,reason)
                    denied=True
                payload['pause_requested']=False
                state='queued';stage=('waiting_storage' if payload.get('wait_reason')=='storage' else 'waiting_provider') if payload.get('retry_at',0)>time.time() else 'pending'
            if not denied:
                # Resume only continues the already accepted grant. It cannot
                # replace a token, change the epoch or broaden past powers.
                conn.execute('UPDATE jobs SET payload=?,state=?,stage=?,updated_at=? WHERE id=?',(db.encode(payload),state,stage,db.utcnow(),job_id))
                db.event(conn,'job_'+action,{'job_id':job_id})
        if denied:raise security.error(409,'authorization_required','Ожидается явное подтверждение прав владельцем')
        return public_job(settings,db.fetchone(settings.db_path,'SELECT * FROM jobs WHERE id=?',(job_id,)))

    @app.post('/api/jobs/{job_id}/reauthorize')
    def reauthorize(request:Request,job_id:str,body:automation.ReauthorizeInput):
        active=security.require_session(request,settings,True,owner_only=True)
        with db.transaction(settings.db_path) as conn:
            row=conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row:raise security.error(404,'job_not_found','Задание не найдено')
            if row['state']!='waiting_authorization' or row['cancel_requested']:
                raise security.error(409,'job_not_waiting_authorization','Задание не ожидает новых прав или еще завершает текущую операцию')
            if db.setting(settings.db_path,'recovery_required','false')=='true':raise security.error(409,'recovery_required','Сначала завершите восстановление')
            epoch=int((conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone() or {'value':'0'})['value'])
            if active['auth_epoch']!=epoch:raise security.error(409,'authorization_epoch_changed','Авторизация владельца изменилась')
            required=automation.required_scopes(row['kind'])
            if not required or len(body.scopes)!=len(set(body.scopes)) or set(body.scopes)!=set(required):
                raise security.error(422,'invalid_scopes','Явно выберите только необходимые права этого задания')
            payload=json.loads(row['payload'])
            if body.authorization_version!=payload.get('authorization_version',0):
                raise security.error(409,'authorization_version_conflict','Ожидание прав изменилось; обновите задание')
            previous={'token_id':payload.pop('auth_token_id',None),'auth_epoch':row['auth_epoch']}
            payload.update(auth_scopes=sorted(body.scopes),auth_limited=True,authorization_version=body.authorization_version+1)
            payload.pop('authorization_wait',None)
            state='paused' if payload.get('pause_requested') else 'queued'
            stage='paused' if state=='paused' else (
                ('waiting_storage' if payload.get('wait_reason')=='storage' else 'waiting_provider')
                if payload.get('retry_at',0)>time.time() else 'pending')
            conn.execute('UPDATE jobs SET payload=?,state=?,stage=?,auth_epoch=?,error=NULL,updated_at=? WHERE id=?',
                         (db.encode(payload),state,stage,epoch,db.utcnow(),job_id))
            db.event(conn,'job_reauthorized',{'job_id':job_id,'state':state,'scopes':sorted(body.scopes),
                     'authorization_version':payload['authorization_version'],'previous_grant':previous,'actor':'owner'})
        return public_job(settings,db.fetchone(settings.db_path,'SELECT * FROM jobs WHERE id=?',(job_id,)))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(request: Request, job_id: str):
        active=security.require_session(request, settings, True)
        with db.transaction(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise security.error(404, "job_not_found", "Задание не найдено")
            automation.check_job(active,row['kind'])
            if row["state"] in {"queued", "waiting", "paused", "waiting_authorization"}:
                state = 'cancelled_partial' if row['completed'] or row['failed'] else 'cancelled'
                conn.execute("UPDATE jobs SET state=?,stage=?,cancel_requested=1,error='cancelled',updated_at=? WHERE id=?", (state,state,db.utcnow(),job_id))
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
        providers += [{'id':'youtube','configured':settings.youtube_enabled,'capabilities':['video_provenance','video_search']},
                      {'id':'shazam','configured':settings.external_fingerprints,'capabilities':['fingerprint'],'unofficial':True},
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
            try:
                payload=field_policy.selected_payload(settings,conn,row,chosen,analysis['updated_at'])
                payload=provider_policy.require_payload(conn,row,payload,'cache')
                payload=lyrics_document.propose(conn,row,payload)
            except media.MediaError as exc:
                raise security.error(409,exc.code,exc.message) from exc
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
                try:await asyncio.to_thread(security.require_session,request,settings)
                except HTTPException:break
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
                # Drain replay batches promptly, then poll at a bounded rate.
                # A fixed two-second wait made ordinary progress visibly lag.
                await asyncio.sleep(0 if len(rows) == 100 else .5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "private, no-store"})

    @app.get("/api/openapi.json")
    def openapi(request: Request):
        security.require_session(request, settings)
        return app.openapi()

    api_facades.install(app, settings, JobInput, ProposalInput, create_job, proposal)
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
