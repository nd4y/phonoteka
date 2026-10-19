"""Delivery TTL leases and live shared locks for immutable export downloads."""
import asyncio
from contextlib import suppress
import hashlib
import json
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import db, media, retention, security

TTL = 1800


def actor(active):
    return active.get('api_token_id') or 'owner:' + hashlib.sha256(active['token_hash'].encode()).hexdigest()


def available(settings, export_id):
    try:
        uuid.UUID(export_id)
        path = retention.safe_artifact(settings, 'exports/' + export_id + '/manifest.json')
        doc = json.loads(path.read_text())
        return doc['id'] == export_id and doc.get('snapshot_available') is True
    except (ValueError, KeyError, OSError, media.MediaError):
        return False


def create(settings, export_id, owner, purpose='delivery', ttl=TTL):
    identity = str(uuid.uuid4())
    with db.transaction(settings.db_path) as conn:
        conn.execute("DELETE FROM settings WHERE key LIKE 'export_lease:%' AND json_extract(value,'$.expires_at')<?",(time.time(),))
        if not available(settings, export_id):
            raise security.error(409, 'export_unavailable', 'Экспорт недоступен')
        value = {'id': identity, 'export_id': export_id, 'actor': owner, 'purpose': purpose,
                 'created_at': time.time(), 'expires_at': time.time() + ttl}
        conn.execute('INSERT INTO settings VALUES(?,?)', ('export_lease:' + identity, db.encode(value)))
    return value


def renew(settings, identity, owner, ttl=TTL):
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', ('export_lease:' + identity,)).fetchone()
        value = json.loads(row['value']) if row else None
        if not value or value['actor'] != owner:
            raise security.error(404, 'lease_missing', 'Аренда экспорта не найдена')
        if not available(settings, value['export_id']):
            raise security.error(409, 'export_unavailable', 'Экспорт недоступен')
        value['expires_at'] = time.time() + ttl
        conn.execute('UPDATE settings SET value=? WHERE key=?', (db.encode(value), 'export_lease:' + identity))
    return value


def release(settings, identity, owner):
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', ('export_lease:' + identity,)).fetchone()
        if row and json.loads(row['value'])['actor'] != owner:
            raise security.error(404, 'lease_missing', 'Аренда экспорта не найдена')
        conn.execute('DELETE FROM settings WHERE key=?', ('export_lease:' + identity,))


def safe_view(value):
    return {key: value[key] for key in ('id', 'export_id', 'purpose', 'created_at', 'expires_at')}


class LeasedFileResponse(FileResponse):
    def __init__(self, *args, settings, export_id, lease_actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings, self.export_id, self.lease_actor = settings, export_id, lease_actor

    async def __call__(self, scope, receive, send):
        guard = retention.export_lock(self.settings, self.export_id)
        acquired = False
        lease = None
        task = None
        try:
            try:
                entering = asyncio.create_task(asyncio.to_thread(guard.__enter__))
                try:
                    await asyncio.shield(entering)
                except asyncio.CancelledError:
                    await entering
                    acquired = True
                    raise
                acquired = True
                lease = await asyncio.to_thread(create, self.settings, self.export_id, self.lease_actor, 'download')
            except (BlockingIOError, OSError):
                response = JSONResponse({'code': 'export_busy', 'message': 'Экспорт обслуживается. Повторите загрузку.'}, status_code=503)
                return await response(scope, receive, send)
            except HTTPException as error:
                response = JSONResponse(error.detail, status_code=error.status_code)
                return await response(scope, receive, send)

            async def heartbeat():
                while True:
                    await asyncio.sleep(60)
                    await asyncio.to_thread(renew, self.settings, lease['id'], self.lease_actor)
            task = asyncio.create_task(heartbeat(), name='immutable-export-download-lease')
            await super().__call__(scope, receive, send)
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if acquired:
                guard.__exit__(None, None, None)
            if lease:
                # A failed cleanup retains a bounded TTL lease; it cannot expose
                # or delete data. The OS lock covers the complete live response.
                with suppress(Exception):
                    await asyncio.to_thread(release, self.settings, lease['id'], self.lease_actor)


class StartInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    export_id: str = Field(min_length=36, max_length=36)


def install(app, settings):
    def access(request):
        active = security.require_session(request, settings, True)
        from . import automation
        automation.check_scope(active, 'publish:write', 'Токен не разрешает доставку')
        return active

    @app.post('/api/export-leases', status_code=201)
    def start(request: Request, body: StartInput):
        return safe_view(create(settings, body.export_id, actor(access(request))))

    @app.post('/api/export-leases/{identity}/renew')
    def extend(request: Request, identity: str):
        return safe_view(renew(settings, identity, actor(access(request))))

    @app.delete('/api/export-leases/{identity}')
    def finish(request: Request, identity: str):
        release(settings, identity, actor(access(request)))
        return {'released': True}
