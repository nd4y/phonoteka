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
        canonical = old.get('preferred_lyrics_slot') if kind == 'lyrics' else old.get('preferred_cover', {}).get('native_identity')
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
        metadata['lyrics_selection_empty'] = not bool(metadata['lrc'])
    elif old.get('preferred_lyrics_slot') or old.get('lyrics_selection_empty'):
        for field in ('preferred_lyrics_slot', 'lyrics_selection_empty', 'lrc', 'lyrics'):
            if field in old: metadata[field] = old[field]
    if payload.get('cover') and selected.get('cover'):
        chosen = next((slot for slot in metadata.get('slot_inventory', {}).get('slots', []) if slot['kind'] == 'cover' and slot['native_identity'] == selected['cover']), None)
        if not chosen: raise media.MediaError('slot_conflict', 'The committed cover target is absent from the reopened inventory')
        metadata['preferred_cover'] = metadata_slots.reference(chosen)
    elif old.get('preferred_cover'):
        metadata['preferred_cover'] = old['preferred_cover']
    if payload.get('cover') and payload.get('artwork_transform'):
        metadata['artwork_transform'] = payload['artwork_transform']
    elif not payload.get('cover') and old.get('artwork_transform'):
        metadata['artwork_transform'] = old['artwork_transform']
    return metadata


def canonical_snapshot(metadata):
    identity = metadata.get('preferred_lyrics_slot')
    result = {'native_identity': identity, 'text_sha256': metadata_slots.digest(metadata.get('lrc') or metadata.get('lyrics') or ''),
              'synchronized': bool(metadata.get('lrc'))} if identity else {}
    if metadata.get('preferred_cover'): result['cover'] = metadata['preferred_cover']
    if metadata.get('lyrics_selection_empty'): result['lyrics_selection_empty'] = True
    return result


def restore_canonical(path, metadata, snapshot):
    if not snapshot: return metadata
    audio = media._load_audio(path)
    if snapshot.get('cover'):
        metadata_slots.resolve(audio, snapshot['cover'], 'cover')
        metadata['preferred_cover'] = snapshot['cover']
    if snapshot.get('lyrics_selection_empty'):
        metadata.update(lrc='', lyrics='', lyrics_selection_empty=True)
        metadata.pop('preferred_lyrics_slot', None)
        return metadata
    if not snapshot.get('native_identity'): return metadata
    target = next((slot for slot in metadata_slots.native(audio) if slot['kind'] == 'lyrics'
                   and slot['native_identity'] == snapshot.get('native_identity')
                   and metadata_slots.digest(slot.get('text', '')) == snapshot.get('text_sha256')), None)
    if not target: raise media.MediaError('slot_conflict', 'Restored bytes do not contain the prior selected lyric document')
    metadata['preferred_lyrics_slot'] = target['native_identity']
    metadata['lrc'], metadata['lyrics'] = (target['text'], '') if snapshot.get('synchronized') else ('', target['text'])
    return metadata


def canonical_sources(settings, row, *, verify_files=True, hint=None):
    """Resolve current metadata against actual exact-revision native content."""
    metadata = json.loads(row['metadata']); result = {'cover': None, 'lyrics': None}
    audio = read(settings, row)[0] if verify_files else None
    entries = metadata_slots.native(audio) if verify_files else metadata.get('slot_inventory', {}).get('slots', [])
    if metadata.get('preferred_cover'):
        if verify_files: result['cover'] = metadata_slots.reference(metadata_slots.resolve(audio, metadata['preferred_cover'], 'cover'))
        else:
            found = next((slot for slot in entries if slot['kind'] == 'cover' and metadata_slots.reference(slot) == metadata['preferred_cover']), None)
            if not found: raise media.MediaError('slot_conflict', 'Stored canonical cover selection is stale')
            result['cover'] = metadata['preferred_cover']
    else:
        cover = next((slot for slot in entries if slot['kind'] == 'cover' and slot.get('editable')), None)
        if cover: result['cover'] = metadata_slots.reference(cover)
    raw = metadata.get('lrc') or metadata.get('lyrics') or ''
    if raw and not metadata.get('lyrics_selection_empty'):
        target = metadata.get('preferred_lyrics_slot')
        raw_hash = metadata_slots.digest(raw); previous = (hint or {}).get('lyrics') or {}
        found = next((slot for slot in entries if slot['kind'] == 'lyrics' and
                      (slot.get('text_sha256') == raw_hash or not verify_files and not slot.get('text_sha256') and
                       slot['id'] == previous.get('slot_id') and raw_hash == previous.get('content_sha256'))
                      and (target is None or slot['native_identity'] == target)), None)
        if not found: raise media.MediaError('slot_conflict', 'The reviewed canonical lyric document is absent from current native slots')
        result['lyrics'] = metadata_slots.reference({**found, 'text_sha256': raw_hash})
    if verify_files:
        media._expect(media._validate(settings.work_root / row['relative_path'], settings.roots), row['sha256'])
    return result


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
