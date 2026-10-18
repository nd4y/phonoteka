"""Native artwork/lyric slot inventory and exact, revision-bound replacement.

Slot IDs are content fingerprints, not mutable array positions supplied by a
client. Unsupported slots remain visible and are never rewritten implicitly.
"""
import base64
import hashlib
import json

from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, SYLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE
from . import formats

MAX_SLOTS = 256


def fail(code, message):
    from .media import MediaError
    raise MediaError(code, message)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def native(audio):
    result = []
    def add(kind, identity, content, **values):
        raw = content if isinstance(content, bytes) else str(content).encode('utf-8')
        result.append({'id': digest([kind, identity, digest(raw)]), 'kind': kind, 'native_identity': identity,
                       'sha256': digest(raw), 'size': len(raw), **values})
    if isinstance(audio, (MP3, WAVE, AIFF)):
        for frame in audio.tags.values() if audio.tags else []:
            if isinstance(frame, APIC):
                data_hash = digest(frame.data)
                headers = json.dumps([int(frame.encoding), frame.mime, int(frame.type), frame.desc, data_hash], ensure_ascii=False)
                add('cover', frame.HashKey, headers, native_type='APIC', picture_type=int(frame.type),
                    description=frame.desc, mime=frame.mime, editable=frame.type == 3, _frame=frame,
                    data_sha256=data_hash, size=len(frame.data))
            elif frame.FrameID in {'USLT', 'SYLT'}:
                timed = frame.FrameID == 'SYLT'
                supported = timed and frame.format == 2 and frame.type == 1
                text = str(frame.text) if not timed else ''
                if supported:
                    from .media import serialize_lrc
                    try: text = serialize_lrc([{'text': words, 'start_ms': timestamp} for words, timestamp in frame.text])
                    except Exception:
                        # A malformed native frame stays readable as a slot,
                        # but cannot become a replacement target.
                        supported = False; text = str(frame.text)
                add('lyrics', frame.HashKey, repr(frame), native_type=frame.FrameID, language=frame.lang,
                    description=frame.desc, synchronized=timed, editable=supported, text=text, _frame=frame)
    elif isinstance(audio, FLAC):
        for index, picture in enumerate(audio.pictures):
            add('cover', 'picture:' + str(index), picture.write(), native_type='FLAC_PICTURE', picture_type=picture.type,
                description=picture.desc, mime=picture.mime, editable=picture.type == 3, _index=index, _picture=picture,
                data_sha256=digest(picture.data))
    if isinstance(audio, MP4):
        for index, cover in enumerate(audio.get('covr', [])):
            add('cover', 'covr:' + str(index), str(cover.imageformat).encode() + bytes(cover), native_type='MP4_COVR', picture_type=None,
                description='Artwork ' + str(index + 1), mime='image/png' if cover.imageformat == MP4Cover.FORMAT_PNG else 'image/jpeg',
                editable=True, _index=index, data_sha256=digest(bytes(cover)), size=len(cover))
    elif isinstance(audio, (formats.OggVorbis, formats.OggOpus)):
        for index, raw in enumerate(audio.get('metadata_block_picture', [])):
            try:
                picture = Picture(base64.b64decode(raw, validate=True))
                add('cover', 'metadata_block_picture:' + str(index), raw, native_type='OGG_PICTURE', picture_type=picture.type,
                    description=picture.desc, mime=picture.mime, editable=picture.type == 3, _index=index, _picture=picture,
                    data_sha256=digest(picture.data))
            except (ValueError, TypeError):
                add('cover', 'metadata_block_picture:' + str(index), raw, native_type='OGG_PICTURE', editable=False,
                    description='Malformed picture block', error='invalid_picture', _index=index)
    if isinstance(audio, (FLAC, *formats.PROFILES)):
        names = ['©lyr'] if isinstance(audio, MP4) else [key for key in audio if key.casefold() in {'lyrics', 'unsyncedlyrics', 'syncedlyrics'}]
        for name in names:
            for index, text in enumerate(audio.get(name, [])):
                text = str(text)
                add('lyrics', name + ':' + str(index), text, native_type='MP4_LYRICS' if isinstance(audio, MP4) else 'VORBIS_COMMENT',
                    description=name + ' [' + str(index + 1) + ']', language=None, synchronized=bool('[' in text and ':' in text),
                    editable=name == '©lyr' or name.casefold() == 'lyrics', text=text, _key=name, _index=index)
    return result


def inventory(audio, *, text=False):
    entries = native(audio)
    visible = []; remaining_text = 2 * 1024 * 1024
    for item in entries[:MAX_SLOTS]:
        entry = {key: value for key, value in item.items() if not key.startswith('_') and key != 'text'}
        if text and item['kind'] == 'lyrics':
            content = item.get('text', '')
            entry['text'] = content[:remaining_text]
            entry['text_truncated'] = len(entry['text']) != len(content)
            remaining_text -= len(entry['text'])
        visible.append(entry)
    return {'inventory_sha256': digest(sorted(item['id'] for item in entries)), 'slots': visible,
            'truncated': len(entries) > MAX_SLOTS, 'total': len(entries), 'new_slot_id': 'new'}


def targets(audio, selection=None, *, lyrics=False, cover=False):
    selection = selection or {}
    known = {'inventory_sha256', 'lyrics_slot_id', 'cover_slot_id'}
    if not isinstance(selection, dict) or set(selection) - known or any(not isinstance(v, str) for v in selection.values()):
        fail('invalid_slot_selection', 'Invalid native slot selection')
    entries = native(audio)
    if len(entries) > MAX_SLOTS: fail('slot_inventory_too_large', 'Too many native metadata slots; manual review required')
    if selection and selection.get('inventory_sha256') != digest(sorted(item['id'] for item in entries)):
        fail('slot_conflict', 'Native metadata slots changed; reopen the inventory')
    result = {}
    for kind, wanted in [('lyrics', lyrics), ('cover', cover)]:
        if not wanted: continue
        candidates = [item for item in entries if item['kind'] == kind and item['editable']]
        identity = selection.get(kind + '_slot_id')
        if identity == 'new':
            if kind == 'cover' and candidates: fail('cover_conflict', 'Choose an existing front cover to replace')
            result[kind] = None
        elif identity:
            matches = [item for item in candidates if item['id'] == identity]
            if len(matches) != 1: fail('slot_conflict', 'Selected slot is missing, changed or not writable')
            if len(matches[0]['native_identity']) > 2048: fail('slot_conflict', 'Native slot identity is too large for durable journaling')
            result[kind] = matches[0]
        elif len(candidates) > 1:
            fail(kind + '_conflict', 'Multiple native slots require an explicit selection')
        else: result[kind] = candidates[0] if candidates else None
    return result


def untouched(audio, selection=None, *, lyrics=False, cover=False):
    chosen = targets(audio, selection, lyrics=lyrics, cover=cover)
    removed = {item['id'] for item in chosen.values() if item}
    return [item['id'] for item in native(audio) if item['id'] not in removed]


def preservation(audio, chosen):
    excluded = {(kind, item['native_identity']) for kind, item in chosen.items() if item}
    return sorted((item['kind'], item['native_type'], item['sha256']) for item in native(audio)
                  if (item['kind'], item['native_identity']) not in excluded)


def planned_targets(audio, chosen):
    result = dict(chosen)
    for kind, target in chosen.items():
        if target: continue
        if isinstance(audio, (MP3, WAVE, AIFF)):
            identity = SYLT(lang='und', desc='Phonoteka', format=2, type=1, text=[]).HashKey if kind == 'lyrics' else APIC(desc='Phonoteka front cover').HashKey
        elif kind == 'lyrics':
            name = '©lyr' if isinstance(audio, MP4) else 'lyrics'
            identity = name + ':' + str(len(audio.get(name, [])))
        elif isinstance(audio, FLAC): identity = 'picture:' + str(len(audio.pictures))
        else:
            name = 'covr' if isinstance(audio, MP4) else 'metadata_block_picture'
            identity = name + ':' + str(len(audio.get(name, [])))
        result[kind] = {'native_identity': identity}
    return result


def verify(audio, chosen, normalized, cover):
    for kind, expected in [('lyrics', normalized), ('cover', cover)]:
        if expected is None: continue
        target = chosen.get(kind)
        matches = [item for item in native(audio) if item['kind'] == kind and
                   (item['native_identity'] == target['native_identity'] if target else True)]
        if kind == 'lyrics':
            valid = any(item.get('text') == normalized for item in matches) if normalized else not matches or any(not item.get('text') for item in matches)
        else:
            valid = any(item.get('data_sha256', item['sha256']) == digest(cover) for item in matches)
        if not valid: fail('verification_failed', 'The selected native slot did not survive independent reopening')


def edit(audio, normalized, cover, cover_info, selection=None):
    chosen = targets(audio, selection, lyrics=normalized is not None, cover=cover is not None)
    if normalized is not None:
        target = chosen['lyrics']
        if isinstance(audio, (MP3, WAVE, AIFF)):
            from .media import parse_lrc
            description, language = (target['description'], target['language']) if target else ('Phonoteka', 'und')
            if target: audio.tags.delall(target['_frame'].HashKey)
            elif audio.tags.getall('SYLT') and any(frame.desc == description and frame.lang == language for frame in audio.tags.getall('SYLT')):
                fail('lyrics_conflict', 'The new lyric slot identity already exists')
            parsed = parse_lrc(normalized)
            if parsed['lines']:
                audio.tags.add(SYLT(encoding=1 if audio.tags.version[1] == 3 else 3, lang=language, format=2, type=1,
                                    desc=description, text=[(line['text'], line['start_ms']) for line in parsed['lines']]))
        else:
            name = target['_key'] if target else ('©lyr' if isinstance(audio, MP4) else 'lyrics')
            values = list(audio.get(name, []))
            if target:
                if normalized: values[target['_index']] = normalized
                else: del values[target['_index']]
            elif normalized: values.append(normalized)
            if values: audio[name] = values
            elif name in audio: del audio[name]
    if cover is not None:
        target = chosen['cover']
        if isinstance(audio, (MP3, WAVE, AIFF)):
            description = target['description'] if target else 'Phonoteka front cover'
            if target: audio.tags.delall(target['_frame'].HashKey)
            elif any(frame.desc == description for frame in audio.tags.getall('APIC')):
                fail('cover_conflict', 'A different picture already owns this description')
            audio.tags.add(APIC(encoding=1 if audio.tags.version[1] == 3 else 3, mime=cover_info[0], type=3, desc=description, data=cover))
        elif isinstance(audio, MP4):
            values = list(audio.get('covr', [])); image = MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG if cover_info[0] == 'image/jpeg' else MP4Cover.FORMAT_PNG)
            if target: values[target['_index']] = image
            else: values.append(image)
            audio['covr'] = values
        else:
            image = Picture(); image.type = 3; image.mime, image.width, image.height = cover_info; image.depth = 24; image.data = cover
            if target and target.get('_picture'): image.desc = target['_picture'].desc
            if isinstance(audio, FLAC):
                values = list(audio.pictures)
                if target: values[target['_index']] = image
                else: values.append(image)
                audio.clear_pictures()
                for picture in values: audio.add_picture(picture)
            else:
                values = list(audio.get('metadata_block_picture', [])); raw = base64.b64encode(image.write()).decode()
                if target: values[target['_index']] = raw
                else: values.append(raw)
                audio['metadata_block_picture'] = values
