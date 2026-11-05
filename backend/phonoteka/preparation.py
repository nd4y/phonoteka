"""One explicit folder-to-review run. Each stage consumes the completed scan."""
import json
import uuid
from typing import Literal

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field

from . import db, media, provider_policy, security, webhooks, work_folders

KEY = "folder_preparation"
STEPS = ("scan", "recognize", "analyze")
DONE = {"candidates", "candidate", "no_match", "fingerprint_match"}


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(default="", max_length=2048)
    profile: Literal["lyrics_required", "instrumental", "relaxed"] = "lyrics_required"


def load(conn):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(conn, run):
    conn.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (KEY, db.encode(run)))
    conn.execute("UPDATE settings SET value=? WHERE key=?", (db.encode(run),"preparation_request:"+run["id"]))


def pending(conn, scan, kind, cursor):
    # Freeze only files actually observed in this scan. Later full scans may
    # supersede observations, but can never introduce an unrelated directory.
    items = []
    for row in conn.execute("""SELECT t.id,t.sha256,t.relative_path,a.payload FROM tracks t
        JOIN job_items j ON j.job_id=? AND j.item_key=t.relative_path AND j.state='succeeded'
        LEFT JOIN analyses a ON a.track_id=t.id AND a.input_sha256=t.sha256
        WHERE t.is_current=1 AND t.status NOT IN ('error','missing') AND t.id>?
        AND t.id=json_extract(j.result,'$.track_id') AND t.sha256=json_extract(j.result,'$.sha256') AND json_extract(t.metadata,'$.format')<>'cue'
        AND COALESCE(json_extract(t.metadata,'$.cue_album_image'),0)=0 ORDER BY t.id""", (scan, cursor)):
        sources = provider_policy.source_states(conn, json.loads(row["payload"] or "{}"))
        complete = (any(sources.get(p) in DONE for p in ("shazam","acoustid")) if kind == "recognize"
                    else all(sources.get(p) in DONE for p in ("musicbrainz","lrclib")))
        if not complete:
            items.append({"track_id": row["id"], "sha256": row["sha256"], "relative_path": row["relative_path"]})
            if len(items) == 10000:
                break
    return items


def advance(conn, settings, run):
    if run.get("state") != "active":
        return
    epoch = int(conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone()[0])
    if epoch != run["epoch"]:
        run.update(state="stopped", reason="authorization_changed")
        return
    if run.get("job_id"):
        job = conn.execute("SELECT state,kind,error,failed,completed FROM jobs WHERE id=?", (run["job_id"],)).fetchone()
        if job and job["state"] in {"queued","running","paused","waiting_authorization","waiting","deferred","waiting_provider","waiting_storage"}:
            return
        partial = job and job["state"] == "completed_with_errors" and job["completed"] > 0
        if partial and job["kind"] == "scan":
            summary = conn.execute("SELECT value FROM settings WHERE key=?", ("last_folder_scan" if run["folder"] else "last_scan",)).fetchone()
            summary = json.loads(summary[0]) if summary else {}
            partial = summary.get("id") == run["job_id"] and summary.get("coverage_complete") is True
        if partial:
            run.setdefault("warnings", {})[job["kind"]] = job["failed"]
        if not job or (job["state"] != "succeeded" and not partial):
            run.update(state="stopped", reason=job["state"] if job else "job_missing")
            return
        if job["kind"] == "scan":
            run["scanned_files"] = job["completed"]
            conn.execute("""INSERT INTO settings(key,value)
                SELECT 'preparation_profile:' || json_extract(result,'$.track_id'), ? FROM job_items
                WHERE job_id=? AND state='succeeded' AND json_extract(result,'$.track_id') IS NOT NULL
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (run["profile"], run["job_id"]))
        run["job_id"] = None
        if not run.pop("more", False):
            run["completed"].append(job["kind"])
            run["cursor"] = ""
    for kind in STEPS:
        if kind in run["completed"] or kind in run["skipped"]:
            continue
        if (kind == "recognize" and not settings.external_fingerprints) or (kind == "analyze" and not settings.external_metadata):
            run["skipped"][kind] = "disabled"
            continue
        items = [] if kind == "scan" else pending(conn, run["scan_id"], kind, run["cursor"])
        if kind != "scan" and not items:
            run["skipped"][kind] = "nothing_pending"
            continue
        if not webhooks.can_accept(conn):
            run["reason"] = "webhook_queue_full"
            return
        identity, stamp = str(uuid.uuid4()), db.utcnow()
        payload = {"items": items, "params": {"folder": run["folder"]} if kind == "scan" else {},
                   "trigger": "preparation", "preparation_id": run["id"]}
        conn.execute("""INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch)
            VALUES(?,?,'queued','pending',?,?,?,?,?)""", (identity, kind, db.encode(payload), len(items), stamp, stamp, epoch))
        db.event(conn, "job_created", {"job_id": identity, "kind": kind, "trigger": "preparation"})
        run.update(job_id=identity, stage=kind, reason=None, more=len(items) == 10000)
        if items:
            run["cursor"] = items[-1]["track_id"]
        if kind == "scan":
            run["scan_id"] = identity
        return
    run.update(state="review", stage="review", reason=None)


def tick(settings):
    with db.transaction(settings.db_path) as conn:
        run = load(conn)
        if run.get("state") == "active":
            previous = db.encode(run)
            advance(conn, settings, run)
            if previous != db.encode(run):
                save(conn, run)


def install(app, settings):
    @app.get("/api/work/folders")
    def folders(request: Request, folder: str = Query("", max_length=2048), after: str = Query("", max_length=255)):
        security.require_session(request, settings, owner_only=True)
        try:
            return work_folders.browse(settings.work_root, folder, after)
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc

    @app.get("/api/preparation")
    def current(request: Request):
        security.require_session(request, settings, owner_only=True)
        with db.connect(settings.db_path) as conn:
            return load(conn)

    @app.post("/api/preparation", status_code=202)
    def start(request: Request, body: Start):
        active = security.require_session(request, settings, True, owner_only=True)
        key = request.headers.get("idempotency-key","")
        if not key or len(key) > 128:
            raise security.error(428, "idempotency_key_required", "Повторите запуск из мастера")
        try:
            work_folders.directory(settings.work_root, body.folder)
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc
        with db.transaction(settings.db_path) as conn:
            run = load(conn)
            if run.get("id") == key:
                if run["folder"] != body.folder or run["profile"] != body.profile:
                    raise security.error(409, "idempotency_conflict", "Запрос с этим ключом уже принят")
                return run
            previous = conn.execute("SELECT value FROM settings WHERE key=?", ("preparation_request:" + key,)).fetchone()
            if previous:
                original = json.loads(previous[0])
                if original["folder"] != body.folder or original["profile"] != body.profile:
                    raise security.error(409, "idempotency_conflict", "Ключ принадлежит другому запуску")
                return original
            if run.get("state") == "active":
                raise security.error(409, "preparation_active", "Подготовка уже запущена. Откройте её ход")
            recovery = conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()
            if recovery and recovery[0] == "true":
                raise security.error(409, "recovery_required", "Сначала завершите проверку восстановления")
            run = {"id": key, "state": "active", "folder": body.folder, "profile": body.profile,
                   "epoch": active["auth_epoch"], "completed": [], "skipped": {}, "cursor": "",
                   "stage": "scan", "job_id": None, "started_at": db.utcnow()}
            advance(conn, settings, run)
            save(conn, run)
            conn.execute("INSERT INTO settings VALUES(?,?)", ("preparation_request:" + key, db.encode(run)))
            return run
