"""Revision-bound duplicate decisions. No audio or personal fields are moved."""
import hashlib
import json
from fastapi import Request, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from . import db, media, security

PREFIX = 'duplicate_decision:'


def group_key(ids):
    return hashlib.sha256(db.encode(sorted(set(ids))).encode()).hexdigest()


def read_tracks(settings, ids):
    result = []
    for identity in ids:
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (identity,))
        if not row:
            raise security.error(404, 'track_missing', 'Один из сравниваемых файлов недоступен')
        meta = json.loads(row['metadata'])
        result.append({'id': identity, 'sha256': row['sha256'], 'path': row['relative_path'],
                       'format': meta.get('format'), 'duration': meta.get('duration'), 'size': meta.get('size'),
                       'codec': meta.get('codec'), 'sample_rate': meta.get('sample_rate'), 'channels': meta.get('channels'),
                       'bitrate': meta.get('bitrate'), 'bits_per_sample': meta.get('bits_per_sample'),
                       'tags': meta.get('tags', {}), 'has_art': bool(meta.get('has_art')), 'has_lrc': bool(meta.get('lrc')),
                       'audit': meta.get('audit'), 'youtube': meta.get('youtube'),
                       'audio_identity': meta.get('audit', {}).get('audio_identity') or meta.get('audio_identity'),
                       'published_sha256': meta.get('publication', {}).get('sha256')})
    return result


def compare(settings, ids):
    tracks = read_tracks(settings, ids)
    identical = len({x['sha256'] for x in tracks}) == 1
    packets = all(x['audio_identity'] for x in tracks) and len({x['audio_identity'] for x in tracks}) == 1
    lengths = [x['duration'] for x in tracks if isinstance(x['duration'], (int, float)) and x['duration'] > 0]
    aligned = bool((identical or packets) and len(lengths) == len(tracks) and max(lengths) - min(lengths) < .1)
    saved = json.loads(db.setting(settings.db_path, PREFIX + group_key(ids), '{}'))
    revisions = {x['id']: x['sha256'] for x in tracks}
    related=[]
    for other in db.fetchall(settings.db_path, 'SELECT key,value FROM settings WHERE key LIKE ?', (PREFIX+'%',)):
        previous=json.loads(other['value'])
        if other['key']!=PREFIX+group_key(ids) and set(previous.get('revisions',{})).intersection(ids):
            related.append({'group_id':other['key'][len(PREFIX):], 'version':previous['version'], 'ids':list(previous['revisions'])})
    return {'tracks': tracks, 'group_id': group_key(ids), 'related_decisions': related, 'classification': 'exact_bytes' if identical else 'same_packets' if packets else 'candidates_only',
            'alignment': {'reliable': aligned, 'offset_seconds': 0 if aligned else None,
                          'reason': 'verified_identical_audio' if aligned else 'no_verified_position_mapping'},
            'decision': saved or None, 'decision_stale': bool(saved and saved.get('revisions') != revisions),
            'version': saved.get('version', 0), 'deletion_available': False}


def publication_choice(settings, track_id):
    for row in db.fetchall(settings.db_path, 'SELECT value FROM settings WHERE key LIKE ?', (PREFIX + '%',)):
        decision = json.loads(row['value'])
        if track_id not in decision.get('revisions', {}):
            continue
        current = {x['id']: x['sha256'] for x in db.fetchall(settings.db_path,
                   'SELECT id,sha256 FROM tracks WHERE is_current=1 AND id IN (%s)' % ','.join('?' for _ in decision['revisions']), tuple(decision['revisions']))}
        if current != decision['revisions']:
            raise media.MediaError('duplicate_decision_stale', 'Файлы группы изменились; повторите выбор предпочтительной копии')
        if decision['mode'] == 'defer':
            raise media.MediaError('needs_review', 'Группа дубликатов отложена владельцем')
        if decision['mode'] == 'preferred' and decision['preferred_id'] != track_id:
            return decision['preferred_id']
    return track_id


def assert_publish_allowed(settings, track_id):
    if publication_choice(settings, track_id) != track_id:
        raise media.MediaError('preferred_copy_selected', 'Для этой группы выбрана другая копия. Проверьте план публикации')


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ids: list[str] = Field(min_length=2, max_length=8)
    revisions: dict[str, str] = Field(max_length=8)
    version: int = Field(ge=0)
    mode: Literal['preferred', 'publish_both', 'defer']
    preferred_id: str | None = None
    supersedes: dict[str, int] = Field(default_factory=dict, max_length=8)


def install(app, settings):
    @app.get('/api/reports/compare')
    def comparison(request: Request, ids: str = Query(max_length=1600)):
        security.require_session(request, settings)
        selected = list(dict.fromkeys(ids.split(',')))
        if not 2 <= len(selected) <= 8:
            raise security.error(422, 'invalid_group', 'Сравните от двух до восьми файлов')
        return compare(settings, selected)

    @app.put('/api/reports/compare/decision')
    def decide(request: Request, body: DecisionInput):
        security.require_session(request, settings, True, owner_only=True)
        ids = list(dict.fromkeys(body.ids))
        if len(ids) != len(body.ids) or len(ids) < 2 or (body.mode == 'preferred' and body.preferred_id not in ids):
            raise security.error(422, 'invalid_group', 'Укажите копию из сравниваемой группы')
        data = compare(settings, ids)
        current = {x['id']: x['sha256'] for x in data['tracks']}
        if body.revisions != current:
            raise security.error(412, 'revision_conflict', 'Сравниваемые файлы изменились')
        key = PREFIX + group_key(ids)
        with db.transaction(settings.db_path) as conn:
            # The decision and the revisions it approves belong to one DB
            # transaction, even if a worker committed after the preview.
            current = {x['id']: x['sha256'] for x in conn.execute(
                'SELECT id,sha256 FROM tracks WHERE is_current=1 AND id IN (%s)' % ','.join('?' for _ in ids), ids)}
            if current != body.revisions:
                raise security.error(412, 'revision_conflict', 'Сравниваемые файлы изменились')
            if conn.execute("SELECT 1 FROM jobs j,json_each(j.payload,'$.items') i WHERE j.kind IN ('apply','rollback','publish','normalize_audio','rollback_ready') AND j.state IN ('queued','running','paused','interrupted') AND json_extract(i.value,'$.track_id') IN (%s) LIMIT 1" % ','.join('?' for _ in ids), ids).fetchone():
                raise security.error(409, 'write_pending', 'Дождитесь завершения записей этой группы')
            superseded=[]
            for other in conn.execute('SELECT key,value FROM settings WHERE key LIKE ?', (PREFIX + '%',)):
                previous = json.loads(other['value'])
                if other['key'] != key and set(previous.get('revisions', {})).intersection(ids):
                    if not set(previous['revisions']).issubset(ids):
                        raise security.error(409, 'overlapping_group', 'Один из файлов уже входит в другую принятую группу. Сравните ее полный состав')
                    if body.supersedes.get(other['key'][len(PREFIX):])!=previous['version']:
                        raise security.error(409, 'decision_conflict', 'Предыдущее решение изменилось; обновите полный состав группы')
                    superseded.append((other['key'],previous))
            row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            old = json.loads(row['value']) if row else {}
            if old.get('version', 0) != body.version:
                raise security.error(409, 'decision_conflict', 'Решение уже изменилось; обновите сравнение')
            document = {'version': body.version + 1, 'mode': body.mode, 'preferred_id': body.preferred_id if body.mode == 'preferred' else None,
                        'revisions': current, 'updated_at': db.utcnow(),
                        'superseded': [{'group_id':k[len(PREFIX):], 'decision':v} for k,v in superseded],
                        'history': [*old.get('history', [])[-19:], {k: v for k, v in old.items() if k != 'history'}] if old else []}
            for superseded_key, previous in superseded:
                conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('duplicate_decision_archive:'+superseded_key[len(PREFIX):]+':'+str(previous['version']), db.encode(previous)))
                conn.execute('DELETE FROM settings WHERE key=?', (superseded_key,))
            conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, db.encode(document)))
            db.event(conn, 'duplicate_decision', {'group_id': group_key(ids), 'version': document['version'], 'mode': body.mode})
        return compare(settings, ids)
