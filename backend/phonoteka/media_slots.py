"""Read-only native slot preview and validation of proposal slot selections."""
import json

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field
from . import db, media, metadata_slots, security


class SlotSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    inventory_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    cover_slot_id: str | None = Field(default=None, pattern='^(new|[a-f0-9]{64})$')
    lyrics_slot_id: str | None = Field(default=None, pattern='^(new|[a-f0-9]{64})$')


def read(settings, row):
    path = media._validate(settings.work_root / row['relative_path'], settings.roots)
    media._expect(path, row['sha256'])
    with media._file(path) as stream:
        audio = media._load_audio(stream)
        if audio is None: raise media.MediaError('invalid_audio', 'Не удалось прочитать метаданные файла')
        return audio, metadata_slots.inventory(audio, text=True)


def validate_proposal(settings, row, payload):
    if payload.get('lrc') is None and not payload.get('cover'): return payload
    try:
        selection = payload.get('slot_selection')
        if selection: selection = {key: value for key, value in selection.items() if value is not None}
        if not selection:
            # Old inventory rows can still make a proposal. The media writer
            # independently refuses ambiguous native slots before any commit.
            inventory = json.loads(row['metadata']).get('slot_inventory', {})
            if inventory.get('truncated'): raise media.MediaError('slot_inventory_too_large', 'Требуется ручная проверка слотов')
            for kind, wanted in [('lyrics', payload.get('lrc') is not None), ('cover', bool(payload.get('cover')))]:
                if wanted and sum(slot['kind'] == kind and slot.get('editable', False) for slot in inventory.get('slots', [])) > 1:
                    raise media.MediaError(kind + '_conflict', 'Выберите один встроенный слот перед записью')
            return payload
        audio, _ = read(settings, row)
        metadata_slots.targets(audio, selection, lyrics=payload.get('lrc') is not None, cover=bool(payload.get('cover')))
        if selection: payload['slot_selection'] = selection
    except media.MediaError as exc:
        raise security.error(412 if exc.code in {'slot_conflict', 'conflict'} else 409, exc.code, exc.message) from exc
    return payload


def committed_metadata(old, metadata, payload, result):
    """Keep the selected lyric document canonical without dropping other rights."""
    selected = result.get('metadata_slot_targets', {})
    provenance = metadata.setdefault('usage_provenance', {})
    old_slots = old.get('slot_inventory', {}).get('slots', [])
    for kind, field in [('lyrics', 'lrc'), ('cover', 'cover')]:
        if field not in payload or payload[field] is None: continue
        previous = old.get('usage_provenance', {}).get(field)
        existing = [slot for slot in old_slots if slot['kind'] == kind]
        compatible = [slot for slot in existing if slot.get('editable')]
        canonical = old.get('preferred_lyrics_slot') if kind == 'lyrics' else None
        if kind == 'lyrics' and not old.get('lrc'):
            ordinary = [slot for slot in existing if slot.get('native_type') == 'USLT']
            canonical = canonical or (ordinary[0]['native_identity'] if ordinary else None)
        canonical = canonical or (compatible[0]['native_identity'] if compatible else None)
        canonical = canonical or (existing[0]['native_identity'] if existing else None)
        target = selected.get(kind)
        if previous and canonical and target and canonical != target:
            provenance['native_slot:' + metadata_slots.digest([kind, canonical])] = previous
    if payload.get('lrc') is not None:
        metadata['lrc'] = media.serialize_lrc(media.parse_lrc(payload['lrc']))
        metadata['lyrics'] = ''
        if selected.get('lyrics') and metadata['lrc']: metadata['preferred_lyrics_slot'] = selected['lyrics']
        elif not metadata['lrc']: metadata.pop('preferred_lyrics_slot', None)
    elif old.get('preferred_lyrics_slot'):
        for field in ('preferred_lyrics_slot', 'lrc', 'lyrics'):
            if field in old: metadata[field] = old[field]
    if payload.get('cover') and payload.get('artwork_transform'):
        metadata['artwork_transform'] = payload['artwork_transform']
    elif not payload.get('cover') and old.get('artwork_transform'):
        metadata['artwork_transform'] = old['artwork_transform']
    return metadata


def canonical_snapshot(metadata):
    identity = metadata.get('preferred_lyrics_slot')
    return {'native_identity': identity, 'text_sha256': metadata_slots.digest(metadata.get('lrc') or metadata.get('lyrics') or ''),
            'synchronized': bool(metadata.get('lrc'))} if identity else {}


def restore_canonical(path, metadata, snapshot):
    if not snapshot: return metadata
    audio = media._load_audio(path)
    target = next((slot for slot in metadata_slots.native(audio) if slot['kind'] == 'lyrics'
                   and slot['native_identity'] == snapshot.get('native_identity')
                   and metadata_slots.digest(slot.get('text', '')) == snapshot.get('text_sha256')), None)
    if not target: raise media.MediaError('slot_conflict', 'Restored bytes do not contain the prior selected lyric document')
    metadata['preferred_lyrics_slot'] = target['native_identity']
    metadata['lrc'], metadata['lyrics'] = (target['text'], '') if snapshot.get('synchronized') else ('', target['text'])
    return metadata


def install(app, settings):
    def row_for(track_id):
        row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=? AND is_current=1', (track_id,))
        if not row: raise security.error(404, 'track_not_found', 'Трек не найден')
        if json.loads(row['metadata']).get('format') == 'cue':
            raise security.error(409, 'cue_read_only', 'Откройте отдельный аудиофайл')
        return row

    @app.get('/api/tracks/{track_id}/slots')
    def slots(request: Request, track_id: str):
        security.require_session(request, settings)
        row = row_for(track_id)
        try: _, inventory = read(settings, row)
        except media.MediaError as exc: raise security.error(409, exc.code, exc.message) from exc
        return {'file_revision': row['sha256'], **inventory}

    @app.get('/api/tracks/{track_id}/slots/{slot_id}/cover')
    def cover(request: Request, track_id: str, slot_id: str):
        security.require_session(request, settings)
        try:
            audio, _ = read(settings, row_for(track_id))
            slot = next((item for item in metadata_slots.native(audio) if item['id'] == slot_id and item['kind'] == 'cover'), None)
            if not slot: raise security.error(404, 'slot_not_found', 'Изображение изменилось или отсутствует')
            if slot.get('error'): raise media.MediaError('invalid_cover', 'Поврежденный слот изображения доступен только в инвентаризации')
            if slot.get('_frame'): raw = slot['_frame'].data
            elif slot.get('_picture'): raw = slot['_picture'].data
            else: raw = bytes(audio.get('covr', [])[slot['_index']])
            mime, _, _ = media._cover(raw)
            return Response(raw, media_type=mime, headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
        except media.MediaError as exc: raise security.error(409, exc.code, exc.message) from exc
