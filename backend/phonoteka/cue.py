"""Read-only external CUE inventory. Times are CD frames (75/s), never PCM samples.

References are local regular audio files inside the work tree. A stale sheet or
dependency must be rescanned before playback. No splitting or tag writes occur.
"""
import hashlib
import math
from pathlib import Path, PurePosixPath
import re

import mutagen

from .media import MediaError

MAX_BYTES = 2 * 1024 * 1024
AUDIO_SUFFIXES = {'.flac', '.wav', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.aiff', '.ape', '.wv', '.wma'}
LOSSLESS_SUFFIXES = {'.flac', '.wav', '.aiff', '.ape', '.wv'}


def fail(message):
    raise MediaError('invalid_cue', message)


def frames(value):
    match = re.fullmatch(r'(\d{1,4}):([0-5]\d):([0-7]\d)', value)
    if not match or int(match[3]) >= 75:
        fail('Некорректное время CUE: ожидается MM:SS:FF, 75 кадров/с')
    return (int(match[1]) * 60 + int(match[2])) * 75 + int(match[3])


def value(text):
    text = text.strip()
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            fail('Незакрытая строка в CUE')
        return text[1:-1]
    return text


def decode(raw):
    if len(raw) > MAX_BYTES:
        fail('CUE превышает 2 МиБ')
    try:
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            return raw.decode('utf-16'), 'utf-16'
        return raw.decode('utf-8-sig'), 'utf-8'
    except UnicodeDecodeError:
        try:
            return raw.decode('cp1251'), 'windows-1251'
        except UnicodeDecodeError:
            fail('Не удалось прочитать кодировку CUE; сохраните файл в UTF-8')


def parse(raw):
    text, encoding = decode(raw)
    if '\x00' in text or len(text.splitlines()) > 20000:
        fail('Некорректный или слишком большой CUE')
    doc = {'encoding': encoding, 'title': '', 'performer': '', 'date': '', 'genre': '', 'files': [], 'tracks': [], 'warnings': []}
    current_file, track = None, None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        # Tabs are also legal separators in CUE sheets.
        parts = line.strip().split(None, 1)
        command, arg = parts[0].upper(), parts[1].strip() if len(parts) > 1 else ''
        if command == 'FILE':
            match = re.fullmatch(r'"([^"\r\n]+)"\s+(\w+)|([^"\s]+)\s+(\w+)', arg)
            if not match or len(doc['files']) >= 99:
                fail(f'Некорректный FILE в строке {line_number}')
            name, kind = (match[1], match[2]) if match[1] else (match[3], match[4])
            if kind.upper() not in {'WAVE', 'MP3', 'AIFF', 'FLAC'}:
                fail('Поддерживаются только музыкальные CUE, без бинарных образов дисков')
            current_file = len(doc['files'])
            doc['files'].append({'declared_name': name, 'declared_type': kind.upper()})
        elif command == 'TRACK':
            match = re.fullmatch(r'(\d{1,2})\s+AUDIO', arg, re.I)
            if not match or current_file is None or not 1 <= int(match[1]) <= 99:
                fail(f'Некорректный AUDIO TRACK в строке {line_number}')
            number = int(match[1])
            if doc['tracks'] and number <= doc['tracks'][-1]['number']:
                fail('Номера треков CUE должны возрастать')
            track = {'number': number, 'title': '', 'performer': '', 'file_index': current_file, 'indexes': {}, 'pregap_frames': 0, 'postgap_frames': 0}
            doc['tracks'].append(track)
        elif command in {'TITLE', 'PERFORMER'}:
            target = track if track is not None else doc
            target[command.lower()] = value(arg)
        elif command == 'INDEX':
            match = re.fullmatch(r'(\d{1,2})\s+(\S+)', arg)
            if track is None or not match:
                fail(f'INDEX вне трека в строке {line_number}')
            index = str(int(match[1]))
            if index in track['indexes']:
                fail('Повторный INDEX внутри трека')
            stamp = frames(match[2])
            track['indexes'][index] = {'frames': stamp, 'file_index': current_file}
            if index == '1':
                track['start_frames'], track['file_index'] = stamp, current_file
        elif command in {'PREGAP', 'POSTGAP'}:
            if track is None:
                fail('PREGAP/POSTGAP вне трека')
            track[command.lower() + '_frames'] = frames(arg)
        elif command == 'REM':
            parts = arg.split(None, 1)
            key, val = (parts[0], parts[1] if len(parts) > 1 else '') if parts else ('', '')
            if track is None and key.upper() in {'DATE', 'GENRE'}:
                doc[key.lower()] = value(val)
        elif command in {'CATALOG', 'CDTEXTFILE', 'ISRC', 'FLAGS', 'SONGWRITER'}:
            # CDTEXTFILE is metadata only and is never opened.
            continue
        else:
            fail(f'Неизвестная команда {command[:30]} в строке {line_number}')
    if not doc['tracks']:
        fail('В CUE нет музыкальных треков')
    for track in doc['tracks']:
        if 'start_frames' not in track:
            fail(f'У трека {track["number"]} нет INDEX 01')
        start = track['start_frames']
        previous = -1
        for index, entry in sorted(track['indexes'].items(), key=lambda x: int(x[0])):
            if entry['file_index'] == track['file_index']:
                if entry['frames'] < previous or (index == '0' and entry['frames'] > start):
                    fail('Индексы трека расположены в неверном порядке')
                previous = entry['frames']
        track['title'] = track['title'] or f'Трек {track["number"]:02d}'
        track['performer'] = track['performer'] or doc['performer']
        track['start_seconds'] = start / 75
    return doc


def safe_path(root, relative, *, must_exist=True):
    name = str(relative).replace('\\', '/')
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or ':' in name or '\x00' in name:
        raise MediaError('unsafe_cue_path', 'Ссылка CUE выходит за рабочую коллекцию')
    root = Path(root).resolve(strict=True)
    target = root
    for part in path.parts:
        target /= part
        if target.is_symlink():
            raise MediaError('unsafe_cue_path', 'Символические ссылки в CUE не поддерживаются')
    if not target.resolve().is_relative_to(root):
        raise MediaError('unsafe_cue_path', 'Ссылка CUE выходит за рабочую коллекцию')
    if must_exist and not target.is_file():
        raise MediaError('cue_file_missing', 'Аудиофайл из CUE не найден')
    return target


def signature(path):
    stat = path.stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'inode': stat.st_ino, 'device': stat.st_dev}


def resolve_reference(root, sheet, name):
    # Validate the declared name before joining it with the sheet's directory.
    safe_path(root, name, must_exist=False)
    relative = (sheet.parent.relative_to(root) / name.replace('\\', '/')).as_posix()
    target = safe_path(root, relative, must_exist=False)
    if target.suffix.lower() not in AUDIO_SUFFIXES:
        raise MediaError('cue_unsupported_audio', 'Тип аудиофайла из CUE не поддерживается')
    if target.is_file():
        return target, None
    matches = []
    if target.parent.is_dir():
        matches = [p for p in target.parent.iterdir() if p.name.casefold() == target.name.casefold() and p.is_file() and not p.is_symlink()]
        if not matches and target.suffix.lower() in LOSSLESS_SUFFIXES:
            matches = [p for p in target.parent.iterdir() if p.stem.casefold() == target.stem.casefold() and p.suffix.lower() in LOSSLESS_SUFFIXES and p.is_file() and not p.is_symlink()]
    if len(matches) != 1:
        raise MediaError('cue_file_ambiguous' if matches else 'cue_file_missing', 'Несколько подходящих файлов' if matches else 'Аудиофайл из CUE не найден')
    return safe_path(root, matches[0].relative_to(root)), 'Ссылка сопоставлена с единственным одноименным аудиофайлом; исходный CUE не изменен'


def inspect_cue(path, root):
    root = Path(root).resolve(strict=True)
    path = safe_path(root, Path(path).relative_to(root))
    if path.stat().st_size > MAX_BYTES:
        fail('CUE превышает 2 МиБ')
    raw = path.read_bytes()
    base = {'format': 'cue', 'entry_type': 'cue_sheet', 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'write_supported': False, 'write_block_reason': 'cue_read_only', 'has_art': False, 'lrc': '', 'lyrics': ''}
    try:
        doc = parse(raw)
    except MediaError as exc:
        return {**base, 'tags': {}, 'cue': {'tracks': [], 'files': [], 'warnings': [], 'errors': [{'code': exc.code, 'message': exc.message}]}}
    doc['errors'] = []
    for source in doc['files']:
        try:
            audio, warning = resolve_reference(root, path, source['declared_name'])
            before = signature(audio)
            parsed = mutagen.File(audio)
            duration = float(parsed.info.length) if parsed is not None and hasattr(parsed, 'info') else 0
            if not math.isfinite(duration) or duration <= 0:
                raise MediaError('cue_audio_unreadable', 'Не удалось прочитать длительность аудио')
            if before != signature(audio):
                raise MediaError('cue_audio_changed', 'Аудиофайл изменился во время чтения')
            source.update({'relative_path': audio.relative_to(root).as_posix(), 'duration': duration, 'signature': before})
            if warning:
                doc['warnings'].append({'file': source['declared_name'], 'message': warning})
        except (MediaError, OSError, mutagen.MutagenError) as exc:
            source['error'] = {'code': getattr(exc, 'code', 'cue_audio_unreadable'), 'message': str(exc) if isinstance(exc, MediaError) else 'Не удалось прочитать аудиофайл'}
            doc['errors'].append({'file': source['declared_name'], **source['error']})
    groups = {}
    for track in doc['tracks']:
        source = doc['files'][track['file_index']]
        track['available'] = 'error' not in source
        if track['available']:
            groups.setdefault(source['relative_path'], []).append(track)
    for tracks in groups.values():
        duration = doc['files'][tracks[0]['file_index']]['duration']
        for i, track in enumerate(tracks):
            end = tracks[i+1]['start_seconds'] if i+1 < len(tracks) else duration
            if track['start_seconds'] >= duration or end <= track['start_seconds'] or end > duration:
                for member in tracks:
                    member['available'] = False
                doc['errors'].append({'code': 'cue_invalid_bounds', 'message': 'Границы треков не соответствуют аудиофайлу'})
                break
            track['end_seconds'], track['duration'] = end, end - track['start_seconds']
    tags = {key: [val] for key, val in {'title': doc['title'] or path.stem, 'album': doc['title'], 'artist': doc['performer'], 'date': doc['date'], 'genre': doc['genre']}.items() if val}
    return {**base, 'tags': tags, 'duration': sum(t.get('duration', 0) for t in doc['tracks'] if t['available']), 'cue': doc}


def playback_path(root, sheet_relative, metadata, number):
    if metadata.get('format') != 'cue':
        raise MediaError('cue_track_unavailable', 'Запись не является CUE')
    sheet = safe_path(root, sheet_relative)
    if sheet.stat().st_size > MAX_BYTES or hashlib.sha256(sheet.read_bytes()).hexdigest() != metadata['sha256']:
        raise MediaError('cue_stale', 'CUE изменился. Просканируйте коллекцию заново')
    document = metadata.get('cue', {})
    track = next((t for t in document.get('tracks', []) if t['number'] == number), None)
    if not track or not track.get('available'):
        raise MediaError('cue_track_unavailable', 'Композиция CUE недоступна')
    source = document['files'][track['file_index']]
    audio = safe_path(root, source['relative_path'])
    if signature(audio) != source['signature']:
        raise MediaError('cue_stale', 'Аудиофайл изменился. Просканируйте коллекцию заново')
    return audio
