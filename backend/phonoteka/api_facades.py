"""Named v1 resources reuse the same bounded jobs and immutable proposals."""
import hashlib
import json
import uuid
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from . import automation, db, lyrics_review, media, publication, publication_paths, security


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def load(conn, identity):
    row = conn.execute('SELECT value FROM settings WHERE key=?', ('publication_plan:' + identity,)).fetchone()
    if not row:
        raise media.MediaError('publication_plan_missing', 'План публикации не найден')
    value = json.loads(row['value'])
    if digest({key: part for key, part in value.items() if key != 'plan_sha256'}) != value['plan_sha256']:
        raise media.MediaError('publication_plan_corrupt', 'Нарушена целостность плана публикации')
    return value


def bind_plan(conn, identity, ids, item):
    plan = load(conn, identity)
    if set(ids) != {member['track_id'] for member in plan['items']}:
        raise media.MediaError('publication_plan_stale', 'Выбранный состав не совпадает с планом публикации')
    member = next(part for part in plan['items'] if part['track_id'] == item['track_id'])
    for field in ('sha256', 'relative_path', 'publication_review', 'publication_target'):
        if member[field] != item[field]:
            raise media.MediaError('publication_plan_stale', 'Ревизия, профиль, решение или Ready изменились после создания плана')
    item['publication_plan_sha256'] = plan['plan_sha256']


def validate_plan(settings, value, *, skip=()):
    paths = publication_paths.Index()
    for member in value['items']:
        publication_paths.add(paths, member['publication_target']['ready_path'])
        if member['track_id'] in skip:
            continue
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (member['track_id'],))
        if not row or row['sha256'] != member['sha256'] or row['relative_path'] != member['relative_path']:
            raise media.MediaError('publication_plan_stale', 'Рабочая ревизия плана публикации изменилась')
        media._expect(media._validate(settings.work_root / row['relative_path'], settings.roots), member['sha256'])
        lyrics_review.assert_publish_allowed(settings, row, frozen=member['publication_review'])
        publication.assert_target(settings, row, member['publication_target'])
    # Ready is a shared portable namespace, including audio outside this batch.
    for row in db.fetchall(settings.db_path, "SELECT metadata FROM tracks WHERE json_extract(metadata,'$.publication.sha256') IS NOT NULL"):
        relative = json.loads(row['metadata'])['publication']['relative_path']
        if paths.get(publication_paths.key(relative)) != relative:
            publication_paths.add(paths, relative)
    for row in db.fetchall(settings.db_path, "SELECT value FROM settings WHERE key LIKE 'sidecar_object:%'"):
        publication_paths.add(paths, json.loads(row['value'])['path'])
    root = settings.publication_root / 'Ready'
    actual = publication_paths.Index()
    for index, path in enumerate(root.rglob('*')):
        if index >= 100000:
            raise media.MediaError('publication_namespace_limit', 'Превышен предел проверки путей Ready')
        media._no_links(path)
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            normalized = publication_paths.add(actual, relative)
            if normalized in paths and paths[normalized] != relative:
                raise media.MediaError('publication_path_conflict', 'Регистр/Unicode Ready расходится с планом')
    for member in value['items']:
        normalized = publication_paths.key(member['publication_target']['ready_path'])
        parts = normalized.split('/')
        if normalized in actual.directories or any('/'.join(parts[:index]) in actual for index in range(1, len(parts))):
            raise media.MediaError('publication_path_conflict', 'План Ready конфликтует с файлом или каталогом')


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    acceptance_mode: Literal['manual', 'automatic'] = 'manual'


class PlanInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    track_ids: list[str] = Field(min_length=1, max_length=200)
    profile: Literal['lyrics_required', 'relaxed', 'instrumental'] | None = None


class CommitInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_sha256: str = Field(pattern='^[a-f0-9]{64}$')


def install(app, settings, job_model, proposal_model, create_job, save_proposal):
    def current_proposal(identity):
        value = db.fetchone(settings.db_path, 'SELECT * FROM proposals WHERE id=?', (identity,))
        if not value:
            raise security.error(404, 'proposal_missing', 'Предложение не найдено')
        return value

    def require_revision(request, expected):
        supplied = request.headers.get('if-match')
        if not supplied:
            raise security.error(428, 'precondition_required', 'Передайте ожидаемую ревизию If-Match')
        if supplied.strip('"') != expected:
            raise security.error(412, 'revision_conflict', 'Ревизия изменилась; обновите ресурс')

    def replay(request, active, body):
        scope = 'token:' + active['api_token_id'] + ':jobs' if active.get('api_token_id') else 'owner:jobs'
        previous = db.fetchone(settings.db_path, 'SELECT key FROM idempotency WHERE scope=? AND key=?', (scope, request.headers.get('idempotency-key', '')))
        # The canonical job handler checks the body hash and revocation again.
        return create_job(request, body) if previous else None

    @app.patch('/api/proposals/{proposal_id}', status_code=201)
    def patch_proposal(request: Request, proposal_id: str, body: proposal_model):
        security.require_session(request, settings, True)
        selected = current_proposal(proposal_id)
        if body.base_proposal_id not in (None, proposal_id):
            raise security.error(422, 'proposal_conflict', 'Базовое предложение должно совпадать с URL')
        merged = proposal_model(**{**body.model_dump(exclude_unset=True), 'base_proposal_id': proposal_id})
        return save_proposal(request, selected['track_id'], merged)

    @app.post('/api/proposals/{proposal_id}/apply', status_code=202)
    def apply_proposal(request: Request, proposal_id: str, body: ApplyInput):
        active = security.require_session(request, settings, True)
        selected = current_proposal(proposal_id)
        operation = job_model(kind='apply', track_ids=[selected['track_id']], params={'proposal_id': proposal_id, 'acceptance_mode': body.acceptance_mode})
        previous = replay(request, active, operation)
        if previous is not None:
            return previous
        require_revision(request, selected['input_sha256'])
        return create_job(request, operation)

    @app.post('/api/publications/plan', status_code=201)
    def plan_publication(request: Request, body: PlanInput):
        active = security.require_session(request, settings, True)
        automation.check_scope(active, 'publish:write')
        key = request.headers.get('idempotency-key', '')
        if not key or len(key) > 128:
            raise security.error(428, 'idempotency_key_required', 'Передайте Idempotency-Key длиной до 128 символов')
        ids = list(dict.fromkeys(body.track_ids))
        if len(ids) != len(body.track_ids):
            raise security.error(422, 'duplicate_tracks', 'Выберите файлы без повторов')
        scope = active.get('api_token_id', 'owner')
        memo_key = 'publication_plan_key:' + digest([scope, key])
        request_hash = digest(body.model_dump())
        with db.connect(settings.db_path) as conn:
            memo = conn.execute('SELECT value FROM settings WHERE key=?', (memo_key,)).fetchone()
            if memo:
                previous = json.loads(memo['value'])
                if previous['request_hash'] != request_hash:
                    raise security.error(409, 'idempotency_conflict', 'Этот ключ уже использован для другого плана')
                return load(conn, previous['id'])
        members = []
        paths = publication_paths.Index()
        with db.connect(settings.db_path) as conn:
            for identity in ids:
                row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (identity,)).fetchone()
                if not row:
                    raise security.error(404, 'track_not_found', 'Трек не найден')
                target = publication.freeze_target(row)
                try:
                    publication_paths.add(paths, target['ready_path'])
                    frozen = lyrics_review.freeze_publication(settings, row, conn=conn)
                except media.MediaError as exc:
                    raise security.error(409, exc.code, exc.message) from exc
                if body.profile and body.profile != frozen['profile']:
                    raise security.error(409, 'publication_profile_conflict', 'Выбранный профиль не совпадает с принятым решением файла')
                previous = target['expected_publication'] or {}
                unchanged = previous.get('sha256') == row['sha256'] and previous.get('profile') == frozen['profile'] and previous.get('review_version') == frozen['review_version']
                members.append({'track_id': identity, 'sha256': row['sha256'], 'relative_path': row['relative_path'],
                                'publication_review': frozen, 'publication_target': target,
                                'action': 'skip' if unchanged else 'replace' if previous else 'add'})
        value = {'contract': 'publication-plan-v1', 'id': str(uuid.uuid4()), 'created_at': db.utcnow(), 'items': members,
                 'execution': 'bounded_per_file_journal', 'multi_file_atomic': False}
        try:
            validate_plan(settings, value)
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc
        value['plan_sha256'] = digest(value)
        with db.transaction(settings.db_path) as conn:
            # Revalidate DB dependencies under the save transaction, no file I/O.
            for member in members:
                row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (member['track_id'],)).fetchone()
                if not row or row['sha256'] != member['sha256'] or publication.freeze_target(row) != member['publication_target']:
                    raise security.error(409, 'publication_plan_stale', 'Состав Ready изменился при сохранении плана')
                try:
                    lyrics_review.assert_publication_frozen(settings, row, member['publication_review'], conn=conn)
                except media.MediaError as exc:
                    raise security.error(409, exc.code, exc.message) from exc
            memo = conn.execute('SELECT value FROM settings WHERE key=?', (memo_key,)).fetchone()
            if memo:
                previous = json.loads(memo['value'])
                if previous['request_hash'] != request_hash:
                    raise security.error(409, 'idempotency_conflict', 'Этот ключ уже использован для другого плана')
                return load(conn, previous['id'])
            conn.execute('INSERT INTO settings VALUES(?,?)', ('publication_plan:' + value['id'], db.encode(value)))
            conn.execute('INSERT INTO settings VALUES(?,?)', (memo_key, db.encode({'request_hash': request_hash, 'id': value['id']})))
        return value

    @app.get('/api/publications/{plan_id}')
    def publication_detail(request: Request, plan_id: str):
        security.require_session(request, settings)
        try:
            with db.connect(settings.db_path) as conn:
                return load(conn, plan_id)
        except media.MediaError as exc:
            raise security.error(404, exc.code, exc.message) from exc

    @app.post('/api/publications/{plan_id}/commit', status_code=202)
    def commit_publication(request: Request, plan_id: str, body: CommitInput):
        active = security.require_session(request, settings, True)
        try:
            with db.connect(settings.db_path) as conn:
                value = load(conn, plan_id)
            if body.plan_sha256 != value['plan_sha256']:
                raise media.MediaError('publication_plan_stale', 'Подтвердите точный сохраненный план')
            operation = job_model(kind='publish', track_ids=[item['track_id'] for item in value['items']], params={'publication_plan_id': plan_id})
            previous = replay(request, active, operation)
            if previous is not None:
                return previous
            validate_plan(settings, value)
            return create_job(request, operation)
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc

    @app.post('/api/revisions/{revision_id}/rollback', status_code=202)
    def rollback_revision(request: Request, revision_id: str):
        active = security.require_session(request, settings, True)
        revision = db.fetchone(settings.db_path, 'SELECT * FROM history WHERE id=?', (revision_id,))
        if not revision or revision['kind'] not in {'apply', 'rollback', 'publish', 'rollback_ready'}:
            raise security.error(404, 'revision_missing', 'Изменяемая ревизия не найдена')
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (revision['track_id'],))
        if not row:
            raise security.error(404, 'track_not_found', 'Трек не найден')
        ready = revision['kind'] in {'publish', 'rollback_ready'}
        params = {'history_id': revision_id}
        expected = request.headers.get('if-match', '').strip('"')
        if ready:
            params['expected_ready_sha256'] = expected
        operation = job_model(kind='rollback_ready' if ready else 'rollback', track_ids=[row['id']], params=params)
        automation.check_job(active, operation.kind)
        previous = replay(request, active, operation)
        if previous is not None:
            return previous
        current = (json.loads(row['metadata']).get('publication') or {}).get('sha256') if ready else row['sha256']
        require_revision(request, current)
        return create_job(request, operation)
