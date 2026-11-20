"""One server-owned, finite pass over the exact files observed by the folder scan."""
import csv
import io
import json
import uuid

from fastapi import Request
from fastapi.responses import Response
from . import (album_gain_policy, catalog_sources, confirmed_fields as fields, db,
               enrichment_gaps, field_policy, media, metadata_preparation as prep, preparation,
               provider_policy, review_queue, security, webhooks)

PAGE = 25
REASONS = {**fields.MESSAGES, 'file_revision_changed': 'Файл изменился после сканирования.',
           'fields_present': 'Все требуемые поля присутствуют.', 'child_incomplete': 'Задача обработки не завершилась успешно.',
           'missing_evidence': 'Для заполнения недостающих полей требуется подтверждение.',
           'publication_incomplete': 'Рабочая копия обработана. Публикацию нужно проверить.',
           'cue_requires_extraction': 'Для CUE нужен разбор на отдельные композиции.',
           'cue_album_image': 'Альбомный образ сохраняется целиком. Проверьте связанную CUE-разметку.',
           'retained_source': 'Композиции уже извлечены. Исходник сохранён.'}


def authorize(settings, job):
    from .worker import root_identity
    with db.connect(settings.db_path) as conn:
        run = preparation.load(conn)
        payload = json.loads(job['payload'])
        granted = job['id'] == run.get('job_id') or job['id'] in run.get('publications', {})
        if job['kind'] == 'publish' and payload.get('confirmed_fields_parent'):
            granted = granted or payload['confirmed_fields_parent'] == run.get('job_id')
        if (payload.get('collection_fill_id') != run.get('id')
                or run.get('state') != 'active' or not granted
                or job['auth_epoch'] != run.get('epoch') or root_identity(settings.work_root) != run.get('root')):
            raise media.MediaError('preparation_stopped', 'Заполнение папки остановлено или изменилось подключение')


def capture(settings, job, unchanged):
    with db.transaction(settings.db_path) as conn:
        observations = {v['track_id']: v['sha256'] for v in unchanged.values()}
        errors = 0
        for item in conn.execute('SELECT state,result FROM job_items WHERE job_id=?', (job['id'],)):
            result = json.loads(item['result'])
            if item['state'] == 'succeeded' and result.get('track_id') and result.get('sha256'):
                observations[result['track_id']] = result['sha256']
            else:
                errors += 1
        pairs = sorted(observations.items())
        for start in range(0, len(pairs), PAGE):
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                         (f"fill_page:{job['id']}:{start}", db.encode(pairs[start:start+PAGE])))
        conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                     ('fill_scan:' + job['id'], db.encode({'total': len(pairs), 'errors': errors})))


def after_scan(conn, settings, run):
    row = conn.execute('SELECT value FROM settings WHERE key=?', ('fill_scan:' + run['scan_id'],)).fetchone()
    if not row:
        run.update(state='stopped', reason='scan_manifest_missing')
        return
    observed = json.loads(row[0])
    run.update(stage='autofill', job_id=None, reason=None, processed=0, scanned_files=observed['total'],
               scan_errors=observed['errors'], counts={}, reasons={}, applied=0, aligned=0)
    advance(conn, settings, run)


def _stop_child(conn, run):
    if run.get('batch_id'):
        child = prep.load(conn, run['batch_id'])
        if child.get('state') == 'active':
            child.update(state='stopped', reason='cancelled', finished_at=db.utcnow())
            prep.save(conn, child)
            return child.get('job_id')
    return run.get('job_id')


def cancel(conn, run, reason):
    job = _stop_child(conn, run)
    for identity in {job, *run.get('publications', [])} - {None}:
        conn.execute("""UPDATE jobs SET cancel_requested=1,
                     state=CASE WHEN state='running' THEN state WHEN completed>0 THEN 'cancelled_partial' ELSE 'cancelled' END,
                     updated_at=? WHERE id=? AND state IN ('queued','running','paused','waiting_authorization','waiting_provider',
                     'waiting_storage','waiting','deferred')""", (db.utcnow(), identity))
    run.update(state='stopped', reason=reason, finished_at=db.utcnow())


def advance(conn, settings, run):
    from .worker import root_identity
    if root_identity(settings.work_root) != run['root'] or not settings.writable:
        cancel(conn, run, 'root_or_write_access_changed')
        return
    if run.get('batch_id'):
        child = prep.load(conn, run['batch_id'])
        if child.get('state') == 'active':
            run['job_id'] = child.get('job_id')
            return
        if child.get('state') != 'review':
            for entry in run['page']:
                if entry['id'] in run['participants']:
                    entry['issue'] = child.get('reason') or 'preparation_stopped'
        if child.get('reason') in {'cancelled','authorization_changed','root_identity_changed'}:
            cancel(conn, run, child['reason'])
            return
        run.update(batch_id=None, job_id=None)
    elif run.get('job_id'):
        job = conn.execute('SELECT * FROM jobs WHERE id=?', (run['job_id'],)).fetchone()
        if not job or job['cancel_requested'] or job['state'].startswith('cancelled'):
            cancel(conn, run, 'child_cancelled')
            return
        if job['state'] in prep.WAITING:
            return
        receipts = {r['item_key']:r for r in conn.execute('SELECT * FROM job_items WHERE job_id=?', (job['id'],))}
        for entry in run['page']:
            if entry['id'] not in run['participants']:
                continue
            receipt = receipts.get(entry['id'])
            result = json.loads(receipt['result']) if receipt else {}
            if not receipt or receipt['state'] != 'succeeded':
                entry['issue'] = result.get('code') or 'child_incomplete'
            elif job['kind'] == 'apply' and result.get('before_sha256') == entry['sha'] and result.get('after_sha256'):
                entry['sha'] = result['after_sha256']
                if not entry.get('written'):
                    run['applied'] += 1
                    entry['written'] = True
                followup = (result.get('publication_followup') or {}).get('job_id')
                if followup:
                    run.setdefault('publications', {})[followup] = entry['id']
            elif job['kind'] == 'align':
                run['aligned'] += 1
        run['job_id'] = None
    if run.get('publications'):
        for identity, tid in list(run['publications'].items()):
            job = conn.execute('SELECT state FROM jobs WHERE id=?', (identity,)).fetchone()
            if job and job['state'] in prep.WAITING:
                run['action'] = 'publish'
                return
            for entry in run['page']:
                if entry['id'] == tid:
                    if job and job['state'] == 'succeeded':
                        if not entry.get('published'):
                            run['published'] = run.get('published', 0) + 1
                            entry['published'] = True
                    else:
                        entry['issue'] = 'publication_incomplete'
            del run['publications'][identity]
    if not run.get('page'):
        if run['processed'] >= run['scanned_files']:
            run.update(state='review', stage='review', reason=None, finished_at=db.utcnow())
            return
        stored = conn.execute('SELECT value FROM settings WHERE key=?',
                              (f"fill_page:{run['scan_id']}:{run['processed']}",)).fetchone()
        if not stored:
            cancel(conn, run, 'scan_manifest_missing')
            return
        run['page'] = [{'id': tid, 'sha': sha, 'tried': []} for tid, sha in json.loads(stored[0])]
        conn.executemany('INSERT OR IGNORE INTO settings VALUES(?,?)', (
            ('preparation_profile:' + entry['id'], run['profile']) for entry in run['page']))
    if prep.load(conn).get('state') == 'active' or not webhooks.can_accept(conn):
        run['reason'] = 'waiting_preparation'
        return
    run['reason'] = None
    active = {'auth_epoch': run['epoch']}
    busy, retired = fields.active_tracks(conn), catalog_sources.completed_sources(conn)
    connection = prep.acoustid_connection(settings)
    queue = review_queue.project(conn, settings, [entry['id'] for entry in run['page']])
    selected = []
    reports = []
    for entry in run['page']:
        row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (entry['id'],)).fetchone()
        if not row or row['sha256'] != entry['sha']:
            reports.append((entry['id'], row['relative_path'] if row else entry.get('path', ''), 'review', 'file_revision_changed'))
            continue
        meta = json.loads(row['metadata'])
        entry['path'] = row['relative_path']
        if row['id'] in retired:
            reports.append((entry['id'], row['relative_path'], 'excluded', 'retained_source'))
            continue
        if meta.get('format') == 'cue' or meta.get('cue_album_image'):
            reason = 'cue_requires_extraction' if meta.get('format') == 'cue' else 'cue_album_image'
            reports.append((entry['id'], row['relative_path'], 'review', reason))
            continue
        if not enrichment_gaps.individual_music(meta, row['sha256']):
            reports.append((entry['id'], row['relative_path'], 'excluded', 'excluded_recording'))
            continue
        result = fields.evaluate(settings, conn, row, active, busy=busy, retired=retired)
        kind = None
        value = None
        attempt = None
        if not entry.get('issue') and not result['blocked'] and result['ready']:
            kind, value = 'apply', result['token']
            attempt = 'apply:' + ','.join(sorted(c['field'] for c in result['changes']))
        elif not entry.get('issue'):
            ready = prep.candidate(settings, conn, row, active, result, connection)
            if ready:
                kind, value = ready['kind'], ready['token']
                attempt = ready['receipt']
            action = (queue.get(row['id']) or {}).get('action') or {}
            if (not kind or attempt in entry['tried']) and action.get('kind') == 'align' and row['id'] not in busy and not set(result['blocked']) - prep.SAFE_BLOCKS:
                kind, value = 'align', action['token']
                attempt = 'align'
        attempt = attempt or kind
        if kind and attempt not in entry['tried']:
            if not selected or selected[0][0] == kind:
                selected.append((kind, entry, row, value))
                entry['tried'].append(attempt)
        else:
            missing = enrichment_gaps.missing_fields(meta)
            blockers = result['blocked'] if missing else [b for b in result['blocked'] if b not in {'analysis_missing', 'analysis_stale'}]
            reason = entry.get('issue') or next(iter(blockers), None)
            if missing and not reason:
                reason = next((g['code'] for g in result['remaining'] if g['field'] in missing), 'missing_evidence')
            reports.append((entry['id'], row['relative_path'], 'review' if reason else 'populated', reason or 'fields_present'))
    if selected:
        kind = selected[0][0]
        run['action'] = kind
        run['participants'] = [e['id'] for _, e, _, _ in selected]
        key = str(uuid.uuid4())
        tokens = {e['id']: token for _, e, _, token in selected}
        if kind in {'analyze', 'recognize'}:
            child = prep.start_batch(conn, settings, active, key, tokens)
            run.update(batch_id=key, job_id=child.get('job_id'))
            frozen = prep.load(conn, key)
            frozen['collection_fill_id'] = run['id']
            prep.save(conn, frozen)
        else:
            items = []
            for _, entry, row, token in selected:
                item = {'track_id': row['id'], 'sha256': row['sha256'], 'relative_path': row['relative_path']}
                if kind == 'apply':
                    item.update(fields.freeze(settings, conn, row, active, token))
                    field_policy.require_payload(settings, conn, row, item['proposal'], 'apply')
                    item['proposal'] = provider_policy.api_payload(conn, row, item['proposal'], 'embed')
                    field_policy.require_writable_fields(item['proposal'])
                    album_gain_policy.payload(settings, conn, row, item['proposal'])
                else:
                    review_queue.freeze_inference(conn, row, item, kind, {}, True)
                    item['review_preparation'] = {'token': token, 'classification': json.loads(row['metadata']).get('recording_classification')}
                    conn.execute('INSERT INTO settings VALUES(?,?)', (review_queue.receipt_key(kind,token),db.encode({'job_id':key})))
                items.append(item)
            now = db.utcnow()
            payload = {'items': items, 'collection_fill_id': run['id'], 'params': {'publish_confirmed': True} if kind == 'apply' else {}}
            conn.execute("""INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch)
                            VALUES(?,?,'queued','pending',?,?,?,?,?)""", (key,kind,db.encode(payload),len(items),now,now,run['epoch']))
            db.event(conn, 'job_created', {'job_id': key, 'kind': kind, 'trigger': 'collection_fill'})
            run['job_id'] = key
        return
    for _, _, status, reason in reports:
        run['counts'][status] = run['counts'].get(status, 0) + 1
        run['reasons'][reason] = run['reasons'].get(reason, 0) + 1
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                 (f"fill_report:{run['scan_id']}:{run['processed']}", db.encode(reports)))
    run['processed'] += len(run.pop('page'))
    run['participants'] = []
    run['action'] = None


def install(app, settings):
    @app.get('/api/preparation/{run_id}/report')
    def report(run_id: str, request: Request):
        security.require_session(request, settings, owner_only=True)
        with db.connect(settings.db_path) as conn:
            saved = conn.execute('SELECT value FROM settings WHERE key=?', ('preparation_request:' + run_id,)).fetchone()
            run = json.loads(saved[0]) if saved else {}
            if not run.get('autofill'):
                raise security.error(404, 'preparation_missing', 'Отчёт этого запуска недоступен')
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(('ID', 'Файл', 'Результат', 'Причина', 'Код'))
            labels = {'review': 'Нужно проверить', 'excluded': 'Пропущено', 'populated': 'Поля заполнены'}
            def safe_path(path):
                return "'" + path if path.lstrip().startswith(('=', '+', '-', '@')) or path.startswith(('\t', '\r', '\n')) else path
            for row in conn.execute("SELECT item_key,result FROM job_items WHERE job_id=? AND state<>'succeeded'", (run.get('scan_id'),)):
                issue = json.loads(row['result'])
                writer.writerow((issue.get('track_id', ''), safe_path(row['item_key']), 'Ошибка сканирования',
                                 'Проверьте файл в журнале сканирования.', issue.get('code') or 'scan_error'))
            for row in conn.execute('SELECT value FROM settings WHERE key GLOB ? ORDER BY key', ('fill_report:' + run.get('scan_id', '') + ':*',)):
                for tid, path, status, reason in json.loads(row[0]):
                    writer.writerow((tid, safe_path(path), labels[status], REASONS.get(reason, 'Подробности в задаче обработки.'), reason))
            return Response('\ufeff' + out.getvalue(), media_type='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename="collection.csv"', 'Cache-Control': 'private, no-store'})

    @app.post('/api/preparation/{run_id}/stop')
    def stop(run_id: str, request: Request):
        security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            run = preparation.load(conn)
            if not run.get('autofill') or run.get('id') != run_id:
                raise security.error(409, 'preparation_stale', 'Обновите состояние обработки')
            if run['state'] == 'active':
                cancel(conn, run, 'cancelled')
                preparation.save(conn, run)
            return preparation.public(run)

