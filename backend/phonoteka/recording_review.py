"""A composite recording is an explicit catalog fact, never an automatic split."""
import json
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from . import db, security


def decision(metadata, revision):
    value = metadata.get('recording_classification', {})
    return value if isinstance(value, dict) and value.get('input_sha256') == revision else {}


def possible_mix(metadata, revision):
    current = decision(metadata, revision)
    if current.get('origin') == 'owner':
        return current.get('kind') == 'possible_mix'
    return current.get('kind') == 'possible_mix' or metadata.get('possible_mix') is True


def catalog_status(previous, mixed):
    return 'needs_review' if mixed and previous not in {'missing','error','conflict'} else previous


def annotate(metadata, revision, conflict):
    """A fresh fingerprint never overwrites an owner's revision-bound decision."""
    current = decision(metadata, revision)
    if current.get('origin') != 'owner' and conflict:
        metadata['recording_classification'] = {'kind': 'possible_mix', 'origin': 'segment_conflict',
            'input_sha256': revision, 'version': current.get('version', 0),
            'reason': 'conflicting_segment_recording_ids'}
    metadata['possible_mix'] = possible_mix(metadata, revision)
    metadata['record_type'] = ('cue_sheet' if metadata.get('format') == 'cue' else
        'cue_album_image' if metadata.get('cue_album_image') else
        'possible_mix' if metadata['possible_mix'] else
        'single_recording' if decision(metadata,revision).get('origin') == 'owner' else 'audio')
    return metadata


class ClassificationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str = Field(pattern='^(single_recording|possible_mix)$')
    base_version: int = Field(ge=0)
    reason: str = Field(min_length=5, max_length=1000)


def install(app, settings):
    @app.put('/api/tracks/{track_id}/recording-classification')
    def classify(request: Request, track_id: str, body: ClassificationInput):
        active = security.require_session(request, settings, True, owner_only=True)
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,)).fetchone()
            if not row: raise security.error(404, 'track_not_found', 'Трек не найден')
            if not request.headers.get('if-match'):
                raise security.error(428, 'revision_required', 'Передайте FileRevision')
            if request.headers['if-match'].strip('"') != row['sha256']:
                raise security.error(412, 'revision_conflict', 'Файл изменился')
            if active['auth_epoch'] != int(conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone()[0]):
                raise security.error(401, 'authorization_revoked', 'Сессия отозвана')
            meta = json.loads(row['metadata'])
            if meta.get('format') == 'cue' or meta.get('cue_album_image'):
                raise security.error(409, 'cue_read_only', 'Тип CUE и альбомного образа определяется документом CUE')
            previous = decision(meta, row['sha256'])
            if previous.get('version', 0) != body.base_version:
                raise security.error(412, 'decision_conflict', 'Классификация уже изменена')
            value = {'kind': body.kind, 'origin': 'owner', 'input_sha256': row['sha256'],
                     'version': body.base_version + 1, 'reason': body.reason, 'at': db.utcnow()}
            meta['recording_classification'] = value
            annotate(meta, row['sha256'], False)
            # A single-recording confirmation does not accept tags or lyrics.
            status = catalog_status(row['status'],body.kind == 'possible_mix')
            conn.execute('UPDATE tracks SET metadata=?,status=?,updated_at=? WHERE id=?',
                         (db.encode(meta), status, db.utcnow(), track_id))
            conn.execute('INSERT INTO history VALUES(?,?,NULL,?,?,?,?,?)',
                         (str(uuid.uuid4()), track_id, 'recording_classification', row['sha256'], row['sha256'],
                          db.encode({'before': previous, 'after': value, 'audio_changed': False}), db.utcnow()))
            db.event(conn, 'recording_classified', {'track_id': track_id, 'kind': body.kind, 'version': value['version']})
        return value
