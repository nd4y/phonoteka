"""Opt-in Ready sidecars: frozen group plans, per-object journals and ownership.

Plans contain hashes and references, never a second cache of lyrics/art. Bytes
are reconstructed from the frozen work revision. A group can be visibly partial;
only a completely reconciled group may enter a new immutable export.
"""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image

from . import db, lyrics_review, media, media_slots, metadata_slots, provider_policy, publication, publication_paths, security

MAX_FILES = 200


def digest(value):
    return hashlib.sha256(db.encode(value).encode()).hexdigest()


def saved(conn, key, default=None):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def put(conn, key, value):
    conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, db.encode(value)))


def registry_key(path):
    return 'sidecar_object:' + digest(publication_paths.key(path))


def load_plan(settings, identity):
    try:
        identity = str(uuid.UUID(identity))
    except (ValueError, TypeError):
        raise media.MediaError('sidecar_plan_missing', 'План sidecar не найден')
    value = db.setting(settings.db_path, 'sidecar_plan:' + identity)
    if not value:
        raise media.MediaError('sidecar_plan_missing', 'План sidecar не найден')
    return json.loads(value)


def public_plan(value):
    # No embedded content or absolute filesystem paths leave this module.
    return {key: value[key] for key in ('id', 'created_at', 'state', 'plan_sha256', 'options', 'members', 'objects', 'results', 'error', 'job_id', 'multi_file_atomic')}


def content(settings, member, kind):
    path = media._validate(settings.work_root / member['work_path'], settings.roots)
    media._expect(path, member['work_revision'])
    sources = member.get('canonical_sources')
    if not isinstance(sources, dict):
        raise media.MediaError('sidecar_stale_owner', 'Создайте новый план с явными источниками native-слотов')
    if kind == 'cover':
        if not sources.get('cover'):
            raise media.MediaError('missing_cover', 'Для cover.jpg нужна встроенная front cover')
        raw, mime = media.read_art(path, settings.roots, slot=sources['cover'])
        media._cover(raw)
        if mime == 'image/jpeg':
            data = raw
        else:
            # Explicit, frozen conversion profile, including the alpha background.
            with Image.open(io.BytesIO(raw)) as image:
                rgba = image.convert('RGBA')
                canvas = Image.new('RGB', rgba.size, 'white')
                canvas.paste(rgba, mask=rgba.getchannel('A'))
                output = io.BytesIO()
                canvas.save(output, format='JPEG', quality=95, subsampling=0, optimize=False)
                data = output.getvalue()
        media._cover(data)
        if len(data) > 20 * 1024**2:
            raise media.MediaError('invalid_sidecar', 'JPEG sidecar превышает 20 МиБ')
    else:
        if not sources.get('lyrics'):
            raise media.MediaError('missing_synced_lyrics', 'Для LRC sidecar нужен выбранный синхронизированный текст')
        native = media._load_audio(path)
        raw = metadata_slots.resolve(native, sources['lyrics'], 'lyrics').get('text', '')
        if not raw.strip():
            raise media.MediaError('missing_synced_lyrics', 'Для LRC sidecar нужен встроенный полный синхронизированный текст')
        data = (media.serialize_lrc(media.parse_lrc(raw)) + '\n').encode('utf-8')
        if len(data) > 2 * 1024**2:
            raise media.MediaError('invalid_sidecar', 'LRC превышает 2 МиБ')
    media._expect(path, member['work_revision'])
    return data


def current_member(settings, row, *, verify_files=True, sources_hint=None):
    metadata = json.loads(row['metadata'])
    pub = metadata.get('publication') or {}
    if pub.get('sha256') != row['sha256']:
        raise media.MediaError('ready_revision_required', 'Сначала опубликуйте принятую текущую ревизию аудио')
    gate = lyrics_review.assert_publish_allowed(settings, row)
    if pub.get('review_version') != gate['review_version'] or pub.get('profile') != gate['profile'] or pub.get('document_revision') != gate['document_revision']:
        raise media.MediaError('publication_profile_stale', 'Принятие/профиль изменились после публикации аудио; сначала обновите Ready')
    provider_policy.require_track(settings, row, 'export')
    canonical = media_slots.canonical_sources(settings, row, verify_files=verify_files, hint=sources_hint)
    publication_paths.key(pub['relative_path'])
    if verify_files:
        media._expect(publication.ready_target(settings, pub['relative_path']), pub['sha256'])
        media._expect(media._validate(settings.work_root / row['relative_path'], settings.roots), row['sha256'])
    return {'track_id': row['id'], 'work_path': row['relative_path'], 'work_revision': row['sha256'],
            'ready_path': pub['relative_path'], 'ready_revision': pub['sha256'], 'gate': gate,
            'usage_provenance': metadata.get('usage_provenance', {}), 'canonical_sources': canonical}


def validate_members(settings, plan, *, verify_files=True):
    for frozen in plan['members']:
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (frozen['track_id'],))
        if not row or current_member(settings, row, verify_files=verify_files, sources_hint=frozen.get('canonical_sources')) != frozen:
            raise media.MediaError('conflict', 'Аудио, принятый профиль или происхождение изменились после создания плана')


def namespace(settings, objects):
    paths = publication_paths.Index()
    # Include every catalog audio destination, not just this group's members.
    for row in db.fetchall(settings.db_path, "SELECT metadata FROM tracks WHERE json_extract(metadata,'$.publication.sha256') IS NOT NULL"):
        relative = json.loads(row['metadata'])['publication']['relative_path']
        if paths.get(publication_paths.key(relative)) != relative:
            publication_paths.add(paths, relative)
    for obj in objects:
        publication_paths.add(paths, obj['path'])
    # Detect case/NFC aliases and file/dir conflicts created outside the app.
    root = settings.publication_root / 'Ready'
    actual = publication_paths.Index()
    for index, path in enumerate(root.rglob('*')):
        if index >= 100000:
            raise media.MediaError('publication_namespace_limit', 'Для проверки путей требуется более узкая Ready-коллекция')
        media._no_links(path)
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            normalized = publication_paths.add(actual, relative)
            if normalized in paths and paths[normalized] != relative:
                raise media.MediaError('publication_path_conflict', 'Регистр/Unicode Ready расходится с планом: ' + relative)
    for obj in objects:
        normalized = publication_paths.key(obj['path'])
        parts = normalized.split('/')
        if normalized in actual.directories or any('/'.join(parts[:index]) in actual for index in range(1, len(parts))):
            raise media.MediaError('publication_path_conflict', 'Файл и каталог занимают общий путь: ' + obj['path'])


def build(settings, ids, *, cover=False, lrc=False):
    if not (cover or lrc) or not 1 <= len(ids) <= MAX_FILES or len(set(ids)) != len(ids):
        raise media.MediaError('invalid_sidecar_selection', 'Выберите 1-200 разных треков и хотя бы один sidecar')
    members = []
    for identity in ids:
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (identity,))
        if not row:
            raise media.MediaError('track_not_found', 'Один из выбранных треков недоступен')
        members.append(current_member(settings, row))
    objects = {}
    for member in members:
        for kind, enabled in [('cover', cover), ('lrc', lrc)]:
            if not enabled:
                continue
            path = PurePosixPath(member['ready_path'])
            relative = (path.parent / 'cover.jpg').as_posix() if kind == 'cover' else path.with_suffix('.lrc').as_posix()
            normalized = publication_paths.key(relative)
            data = content(settings, member, kind)
            sha = hashlib.sha256(data).hexdigest()
            provenance = {name: value for name, value in member['usage_provenance'].items() if name == ('cover' if kind == 'cover' else 'lrc')}
            if not provenance:
                provenance = {('cover' if kind == 'cover' else 'lrc'): {'origin': 'preexisting', 'rights_status': 'unknown', 'license_verified': False}}
            owner = {key: member[key] for key in ('work_revision', 'ready_revision', 'ready_path', 'gate', 'canonical_sources')}
            owner['usage_provenance'] = provenance
            if normalized in objects:
                obj = objects[normalized]
                if obj['path'] != relative or kind != 'cover' or obj['kind'] != kind or obj['sha256'] != sha:
                    raise media.MediaError('sidecar_shared_path_conflict', 'Разные треки предлагают несовместимый общий sidecar: ' + relative)
                obj['owners'][member['track_id']] = owner
            else:
                with db.connect(settings.db_path) as conn:
                    baseline = saved(conn, registry_key(relative))
                if baseline:
                    provider_policy.require_export_entry(settings, baseline)
                target = publication.ready_target(settings, relative, missing=True)
                if target.exists() and (not baseline or media.sha256_file(target) != baseline['sha256']):
                    raise media.MediaError('sidecar_untracked_destination', 'Существующий sidecar не совпадает с реестром: ' + relative)
                if baseline and not target.is_file():
                    raise media.MediaError('sidecar_missing_destination', 'Sidecar из реестра отсутствует: ' + relative)
                if baseline and baseline['path'] != relative:
                    raise media.MediaError('publication_path_conflict', 'Нельзя изменить Unicode/регистр пути sidecar')
                if baseline and (baseline['kind'] != kind or kind == 'lrc' and set(baseline['owners']) != {member['track_id']}):
                    raise media.MediaError('sidecar_shared_path_conflict', 'LRC не может принадлежать разным аудиофайлам')
                objects[normalized] = {'path': relative, 'kind': kind, 'sha256': sha, 'size': len(data),
                    'action': 'add' if not baseline else 'unchanged' if baseline['sha256'] == sha else 'replace',
                    'source_track_id': member['track_id'], 'transform': 'jpeg_white_q95_444_v1' if kind == 'cover' else 'normalized_utf8_lrc_v1',
                    'owners': {member['track_id']: owner}, 'expected': baseline}
    selected = set(ids)
    for obj in objects.values():
        old = obj['expected']
        if old and old['sha256'] != obj['sha256'] and not set(old['owners']) <= selected:
            raise media.MediaError('sidecar_owner_conflict', 'Изменение общего cover требует выбрать всех его владельцев: ' + obj['path'])
    values = sorted(objects.values(), key=lambda obj: obj['path'])
    namespace(settings, values)
    return {'members': members, 'objects': values, 'options': {'cover': cover, 'lrc': lrc}}


def create_plan(settings, ids, *, cover=False, lrc=False):
    frozen = build(settings, ids, cover=cover, lrc=lrc)
    identity = str(uuid.uuid4())
    value = {'schema': 1, 'id': identity, 'created_at': db.utcnow(), **frozen,
             'plan_sha256': digest(frozen), 'state': 'planned', 'results': {}, 'error': None, 'job_id': None, 'multi_file_atomic': False}
    with db.transaction(settings.db_path) as conn:
        put(conn, 'sidecar_plan:' + identity, value)
        db.event(conn, 'sidecar_plan_created', {'plan_id': identity, 'objects': len(frozen['objects'])})
    return value


def validate_enqueue(conn, plan_id, ids, job_id):
    plan = saved(conn, 'sidecar_plan:' + plan_id)
    if not plan or set(ids) != {member['track_id'] for member in plan['members']}:
        raise media.MediaError('sidecar_plan_missing', 'Выберите точный состав сохраненного плана')
    if plan['state'] == 'complete':
        raise media.MediaError('sidecar_plan_complete', 'Этот план уже полностью опубликован')
    for member in plan['members']:
        row = conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1', (member['track_id'],)).fetchone()
        if not row or row['sha256'] != member['work_revision']:
            raise media.MediaError('conflict', 'Рабочая ревизия плана sidecar устарела')
    if plan.get('job_id'):
        previous = conn.execute('SELECT state FROM jobs WHERE id=?', (plan['job_id'],)).fetchone()
        if previous and previous['state'] in {'queued', 'running', 'paused', 'waiting', 'waiting_authorization'}:
            raise media.MediaError('sidecar_plan_busy', 'План уже выполняется или ожидает сверки worker')
    plan['job_id'] = job_id
    put(conn, 'sidecar_plan:' + plan_id, plan)


def update_plan(settings, identity, **changes):
    with db.transaction(settings.db_path) as conn:
        value = saved(conn, 'sidecar_plan:' + identity)
        value.update(changes)
        put(conn, 'sidecar_plan:' + identity, value)
    return value


def check_registry(settings, plan, *, verify_files=True):
    with db.connect(settings.db_path) as conn:
        for obj in plan['objects']:
            current = saved(conn, registry_key(obj['path']))
            result = plan['results'].get(obj['path'])
            if result:
                if not current or current.get('plan_id') != plan['id'] or current['sha256'] != obj['sha256']:
                    raise media.MediaError('sidecar_registry_conflict', 'Реестр sidecar изменился после частичного commit')
                if verify_files:
                    media._expect(publication.ready_target(settings, obj['path']), obj['sha256'])
            elif current != obj['expected']:
                raise media.MediaError('sidecar_registry_conflict', 'Другой план изменил владельцев/ревизию общего пути')
            if current:
                provider_policy.require_export_entry(settings, current)


def reconcile(settings, plan, obj, authorize):
    """A matching file alone is not evidence that our operation committed."""
    directory = settings.state_dir / 'backups' / 'sidecars' / plan['id'] / digest(obj['path'])[:24]
    target = publication.ready_target(settings, obj['path'], missing=True)
    before = obj['expected']['sha256'] if obj['expected'] else None
    journals = sorted(directory.glob('*.json')) if directory.exists() else []
    committed = None
    for journal in journals:
        media._no_links(journal)
        document = json.loads(journal.read_text())
        if (document.get('target') != str(target) or document.get('before_sha256') != before or
                document.get('after_sha256') != obj['sha256'] or document.get('action') != 'publish_sidecar'):
            raise media.MediaError('sidecar_journal_conflict', 'Журнал не относится к объекту этого плана')
        authorize()
        recovered = media.recover(journal, settings.roots)
        if recovered['state'] == 'conflict':
            raise media.MediaError('sidecar_journal_conflict', 'Текущее содержимое не подтверждается журналом')
        if recovered['state'] == 'committed':
            if committed:
                raise media.MediaError('sidecar_journal_conflict', 'Несколько committed журналов требуют ручной сверки')
            committed = {'operation_id': recovered['operation_id'], 'sha256': recovered['after_sha256'],
                         'journal_path': str(journal), 'backup_path': recovered.get('backup'), 'recovered': True}
    if committed:
        return committed
    actual = media.sha256_file(target) if target.exists() else None
    if actual != before:
        raise media.MediaError('sidecar_destination_conflict', 'Путь sidecar изменился после плана')
    if actual == obj['sha256']:
        return {'sha256': actual, 'unchanged': True}
    member = next(member for member in plan['members'] if member['track_id'] == obj['source_track_id'])
    raw = content(settings, member, obj['kind'])
    if hashlib.sha256(raw).hexdigest() != obj['sha256']:
        raise media.MediaError('sidecar_content_conflict', 'Детерминированный sidecar не совпал с frozen hash')
    return media.publish_bytes(raw, target, before, settings.roots, settings.publication_root, directory,
                               authorize=authorize, access_source=settings.work_root / member['work_path'])


def record(settings, plan, obj, result):
    with db.transaction(settings.db_path) as conn:
        current = saved(conn, registry_key(obj['path']))
        if current != obj['expected']:
            raise media.MediaError('sidecar_registry_conflict', 'Изменились владельцы пути перед записью реестра')
        owners = {**((current or {}).get('owners', {})), **obj['owners']}
        provenance = dict((current or {}).get('usage_provenance', {})) if current and current['sha256'] == obj['sha256'] else {}
        for identity, owner in obj['owners'].items():
            for field, source in owner.get('usage_provenance', {}).items():
                provenance[identity + ':' + field] = source
        value = {'schema': 1, 'path': obj['path'], 'kind': obj['kind'], 'sha256': obj['sha256'], 'size': obj['size'],
                 'owners': owners, 'usage_provenance': provenance, 'plan_id': plan['id'], 'version': (current or {}).get('version', 0) + 1}
        put(conn, registry_key(obj['path']), value)
        latest = saved(conn, 'sidecar_plan:' + plan['id'])
        latest['results'][obj['path']] = {key: result[key] for key in ('sha256', 'operation_id', 'unchanged', 'recovered') if key in result}
        latest['state'] = 'partial'
        put(conn, 'sidecar_plan:' + plan['id'], latest)
        db.event(conn, 'sidecar_object_committed', {'plan_id': plan['id'], 'kind': obj['kind'], 'objects_committed': len(latest['results'])})
    return latest


def commit(settings, identity, authorize=lambda: None, progress=lambda *args: None):
    plan = load_plan(settings, identity)
    if plan['state'] == 'complete':
        return public_plan(plan)
    authorize()
    validate_members(settings, plan)
    namespace(settings, plan['objects'])
    check_registry(settings, plan)
    # Revalidate every destination before the first new object effect.
    for obj in plan['objects']:
        if obj['path'] not in plan['results']:
            target = publication.ready_target(settings, obj['path'], missing=True)
            actual = media.sha256_file(target) if target.exists() else None
            expected = obj['expected']['sha256'] if obj['expected'] else None
            directory = settings.state_dir / 'backups' / 'sidecars' / plan['id'] / digest(obj['path'])[:24]
            if actual != expected and not any(directory.glob('*.json')):
                raise media.MediaError('sidecar_destination_conflict', 'Конфликт назначения выявлен до первого нового эффекта')
    plan = update_plan(settings, identity, state='committing', error=None)
    try:
        for index, obj in enumerate(plan['objects']):
            if obj['path'] in plan['results']:
                progress(index, obj, 'succeeded')
                continue
            def current_authorization():
                authorize()
                # The complete group is hashed before/after the run. Between
                # effects, recheck all DB gates and the actual owner audio of
                # this object, avoiding quadratic full-collection rereads.
                validate_members(settings, plan, verify_files=False)
                check_registry(settings, plan, verify_files=False)
                for member in plan['members']:
                    if member['track_id'] in obj['owners']:
                        media._expect(media._validate(settings.work_root / member['work_path'], settings.roots), member['work_revision'])
                        media._expect(publication.ready_target(settings, member['ready_path']), member['ready_revision'])
            current_authorization()
            progress(index, obj, 'running')
            result = reconcile(settings, plan, obj, current_authorization)
            plan = record(settings, plan, obj, result)
            progress(index, obj, 'succeeded')
        validate_members(settings, plan)
        check_registry(settings, plan)
        plan = update_plan(settings, identity, state='complete', error=None)
        return public_plan(plan)
    except Exception as exc:
        update_plan(settings, identity, state='partial' if plan['results'] else 'interrupted', error=getattr(exc, 'code', 'internal_error'))
        raise


def export_objects(settings, audio_entries):
    """Fail closed on partial plans or stale owners; retain all owner references."""
    selected = {entry['track_id']: entry for entry in audio_entries}
    result = []
    with db.connect(settings.db_path) as conn:
        for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'sidecar_plan:%'"):
            plan = json.loads(row['value'])
            if plan['state'] in {'partial', 'committing', 'interrupted'} and selected.keys() & {member['track_id'] for member in plan['members']}:
                # A superseding complete plan can reconcile every affected path.
                reconciled = all((saved(conn, registry_key(obj['path'])) or {}).get('plan_id') != plan['id'] and
                    (saved(conn, 'sidecar_plan:' + (saved(conn, registry_key(obj['path'])) or {}).get('plan_id', '')) or {}).get('state') == 'complete'
                    for obj in plan['objects'])
                if not reconciled:
                    raise media.MediaError('sidecar_partial_publication', 'Сначала завершите/сверьте частичную публикацию sidecar')
        for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'sidecar_object:%'"):
            obj = json.loads(row['value'])
            owners = selected.keys() & obj['owners'].keys()
            if not owners:
                continue
            plan = saved(conn, 'sidecar_plan:' + obj['plan_id'])
            if not plan or plan['state'] != 'complete':
                raise media.MediaError('sidecar_partial_publication', 'Группа sidecar не завершена')
            # The shared bytes retain restrictions of every known contributing
            # source, including owners omitted by a preferred-copy decision.
            provenance = dict(obj.get('usage_provenance', {}))
            for identity in owners:
                owner = obj['owners'][identity]
                entry = selected[identity]
                track = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (identity,)).fetchone()
                if (not track or owner['work_revision'] != track['sha256'] or owner['ready_revision'] != entry['sha256'] or
                        owner['ready_path'] != entry['path'] or lyrics_review.publication_gate(settings, track, conn=conn) != owner['gate']):
                    raise media.MediaError('sidecar_stale_owner', 'Ready/профиль изменился: создайте новый план sidecar')
                if owner.get('canonical_sources') != media_slots.canonical_sources(settings, track):
                    raise media.MediaError('sidecar_stale_owner', 'Выбранные native-источники изменились после плана sidecar')
                for field, source in owner.get('usage_provenance', {}).items():
                    provenance[identity + ':' + field] = source
            target = publication.ready_target(settings, obj['path'])
            media._expect(target, obj['sha256'])
            entry = {**obj, 'track_id': None, 'usage_provenance': provenance, 'duration': 0}
            provider_policy.require_export_entry(settings, entry)
            result.append(entry)
    return sorted(result, key=lambda item: item['path'])


def track_state(settings, track):
    pub = json.loads(track['metadata']).get('publication', {})
    objects = []
    with db.connect(settings.db_path) as conn:
        for row in conn.execute("SELECT value FROM settings WHERE key LIKE 'sidecar_object:%'"):
            obj = json.loads(row['value'])
            owner = obj['owners'].get(track['id'])
            if owner:
                plan = saved(conn, 'sidecar_plan:' + obj['plan_id']) or {}
                stale = (owner['work_revision'] != track['sha256'] or owner['ready_revision'] != pub.get('sha256') or
                         owner['ready_path'] != pub.get('relative_path') or
                         lyrics_review.publication_gate(settings, track, conn=conn) != owner['gate'])
                objects.append({'path': obj['path'], 'sha256': obj['sha256'], 'kind': obj['kind'],
                                'state': 'stale_requires_new_plan' if stale else plan.get('state', 'unknown'),
                                'owners': list(obj['owners']), 'plan_id': obj['plan_id']})
    return objects


async def process(worker, job):
    identity = json.loads(job['payload'])['params']['plan_id']
    plan = await worker.io(load_plan, worker.settings, identity)
    with db.transaction(worker.settings.db_path) as conn:
        conn.execute('UPDATE jobs SET total=? WHERE id=?', (len(plan['objects']) + 1, job['id']))
    def progress(index, obj, state):
        worker.item_result(job['id'], 'sidecar:' + str(index), state, {'operation': 'sidecar_' + obj['kind'], 'plan_id': identity})
    try:
        await worker.io(commit, worker.settings, identity, lambda: worker.authorize(job), progress)
        worker.item_result(job['id'], 'sidecar:validation', 'succeeded', {'operation': 'sidecar_group_verified', 'plan_id': identity})
        worker.finish(job['id'], 'succeeded')
    except Exception as exc:
        plan = await worker.io(load_plan, worker.settings, identity)
        failure = worker.failure(exc)
        if plan['state'] == 'planned':
            plan = await worker.io(update_plan, worker.settings, identity, state='blocked', error=failure['code'])
        count = len(plan['results'])
        for index, obj in enumerate(plan['objects']):
            current = db.fetchone(worker.settings.db_path, 'SELECT state FROM job_items WHERE job_id=? AND item_key=?', (job['id'], 'sidecar:' + str(index)))
            if current and current['state'] == 'running':
                worker.item_result(job['id'], 'sidecar:' + str(index), 'needs_review', failure)
        worker.item_result(job['id'], 'sidecar:validation', 'needs_review', failure)
        state = 'cancelled_partial' if worker.cancelled(job['id']) and count else 'cancelled' if worker.cancelled(job['id']) else 'completed_with_errors' if count else 'needs_review'
        worker.finish(job['id'], state, failure['code'])


class PlanInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    track_ids: list[str] = Field(min_length=1, max_length=MAX_FILES)
    cover: bool = False
    lrc: bool = False


def install(app, settings):
    @app.get('/api/publication/sidecars')
    def listing(request: Request):
        security.require_session(request, settings, owner_only=True)
        rows = db.fetchall(settings.db_path, "SELECT value FROM settings WHERE key LIKE 'sidecar_plan:%' ORDER BY json_extract(value,'$.created_at') DESC LIMIT 30")
        return {'default_enabled': False, 'items': [public_plan(json.loads(row['value'])) for row in rows]}

    @app.post('/api/publication/sidecars/plans', status_code=201)
    def create(request: Request, body: PlanInput):
        security.require_session(request, settings, True, owner_only=True)
        try:
            return public_plan(create_plan(settings, body.track_ids, cover=body.cover, lrc=body.lrc))
        except media.MediaError as exc:
            raise security.error(409, exc.code, exc.message) from exc

    @app.get('/api/publication/sidecars/plans/{identity}')
    def detail(request: Request, identity: str):
        security.require_session(request, settings, owner_only=True)
        try:
            return public_plan(load_plan(settings, identity))
        except media.MediaError as exc:
            raise security.error(404, exc.code, exc.message) from exc
