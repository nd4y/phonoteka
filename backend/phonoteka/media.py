"""Local MP3/FLAC operations. Production mutations require Linux local storage.

The worker must hold writer_guard for its lifetime and be the only authorized
writer of work/Ready. Hash checks cannot coordinate an unrelated SMB editor.
Every replacement has a durable, self-contained recovery journal and backup.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, ID3NoHeaderError, APIC, SYLT, TXXX, UFID, Frames
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from mutagen.aiff import AIFF
from . import formats, intake
from PIL import Image, UnidentifiedImageError


class MediaError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


_LRC_TIME = re.compile(r"\[(\d+):([0-5]\d)(?:[.,](\d{1,3}))?\]")
_LRC_META = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*):(.*)\]$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_TEXT_FRAMES = {
    "title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
    "tracknumber": "TRCK", "discnumber": "TPOS", "date": "TDRC", "originaldate": "TDOR",
    "genre": "TCON", "composer": "TCOM", "lyricist": "TEXT",
}
_MB_FIELDS = {"musicbrainz_trackid", "musicbrainz_albumid", "musicbrainz_artistid", "musicbrainz_albumartistid", "musicbrainz_releasetrackid"}
_MB_DESCRIPTIONS = {
    "musicbrainz_trackid": "MusicBrainz Track Id", "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id", "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_releasetrackid": "MusicBrainz Release Track Id",
}
_RG_FIELDS = {'replaygain_track_gain','replaygain_track_peak','replaygain_album_gain','replaygain_album_peak'}
_CUSTOM_FIELDS = _MB_FIELDS | _RG_FIELDS
_MB_DESCRIPTIONS.update({key:key for key in _RG_FIELDS})

MAX_LRC_BYTES, MAX_LRC_LINES = 2 * 1024 * 1024, 20000
MAX_AUDIO_BYTES = 8 * 1024**3
MAX_COVER_BYTES = 15 * 1024 * 1024
_SPACE_MARGIN = 128 * 1024 * 1024
_TAG_GROWTH_BUDGET = 64 * 1024 * 1024
_SPACE_LOCK = threading.Lock()
_SPACE_RESERVED: dict[int, int] = {}


def parse_lrc(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_LRC_BYTES:
        raise MediaError("invalid_lyrics", "LRC must be UTF-8 text smaller than 2 MiB")
    rows = text.lstrip("\ufeff").splitlines()
    if len(rows) > MAX_LRC_LINES:
        raise MediaError("invalid_lyrics", "LRC exceeds 20000 lines")
    metadata: dict[str, list[str]] = {}
    timed, untimed = [], []
    offset: int | None = None
    for index, raw in enumerate(rows):
        meta = _LRC_META.fullmatch(raw)
        if meta and not _LRC_TIME.match(raw):
            key, value = meta.group(1).lower(), meta.group(2)
            metadata.setdefault(key, []).append(value)
            if key == "offset":
                try:
                    parsed_offset = int(value.strip())
                except ValueError as exc:
                    raise MediaError("invalid_lyrics", f"Invalid offset at line {index + 1}") from exc
                if offset is not None:
                    raise MediaError("invalid_lyrics", "Multiple offset declarations are ambiguous")
                offset = parsed_offset
            continue
        pos, stamps = 0, []
        while match := _LRC_TIME.match(raw, pos):
            stamps.append((int(match[1]) * 60 + int(match[2])) * 1000 + int((match[3] or "").ljust(3, "0") or "0"))
            pos = match.end()
        words = raw[pos:]
        if re.search(r"<\d+:\d", words):
            raise MediaError("enhanced_lrc", f"Word timestamps need explicit conversion at line {index + 1}")
        if words.startswith("[") and re.match(r"\[\d", words):
            raise MediaError("invalid_lyrics", f"Malformed timestamp at line {index + 1}")
        if stamps:
            for occurrence, stamp in enumerate(stamps):
                timed.append({"line_id": str(uuid.uuid4()), "text": words, "start_ms": stamp,
                              "timing_state": "imported", "raw_line": index, "occurrence": occurrence})
        elif raw.strip():
            untimed.append({"line_id": str(uuid.uuid4()), "text": raw, "start_ms": None,
                            "timing_state": "untimed", "raw_line": index, "occurrence": 0})
    for line in timed:
        line["start_ms"] -= offset or 0
        if line["start_ms"] < 0:
            raise MediaError("invalid_lyrics", f"Offset gives negative time at line {line['raw_line'] + 1}")
    timed.sort(key=lambda line: (line["start_ms"], line["raw_line"], line["occurrence"]))
    lines = timed + untimed
    if len(lines) > MAX_LRC_LINES:
        raise MediaError("invalid_lyrics", "Expanded LRC exceeds 20000 occurrences")
    for ordinal, line in enumerate(lines):
        line["ordinal"] = ordinal
    return {"lines": lines, "metadata": metadata, "offset_applied": offset or 0,
            "complete": bool(timed) and not untimed, "needs_placement": bool(timed and untimed)}


def serialize_lrc(lines: list[dict[str, Any]] | dict[str, Any], *, allow_partial: bool = False) -> str:
    if isinstance(lines, dict):
        lines = lines["lines"]
    output, previous = [], -1
    for line in lines:
        timestamp, words = line.get("start_ms"), line.get("text", "")
        if not isinstance(words, str) or "\n" in words or "\r" in words:
            raise MediaError("invalid_lyrics", "Each lyric row must contain one line of text")
        if timestamp is None:
            if words.strip() and not allow_partial:
                raise MediaError("partial_lyrics", "Untimed words cannot be exported as fully synchronized lyrics")
            continue
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0 or timestamp < previous:
            raise MediaError("invalid_lyrics", "Timestamp order is invalid; rows were not silently reordered")
        previous = timestamp
        minutes, remainder = divmod(timestamp, 60000)
        seconds, millis = divmod(remainder, 1000)
        output.append(f"[{minutes:02}:{seconds:02}.{millis:03}]{words}")
    return "\n".join(output)


def _absolute(path: str | Path) -> Path:
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts:
        raise MediaError("invalid_path", "An absolute canonical path is required")
    return value


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _no_links(path: Path, *, missing: bool = False) -> None:
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise MediaError("missing_file", "A configured file or directory is unavailable") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise MediaError("unsafe_path", "Symbolic links and reparse points are not allowed")


def _validate(path: str | Path, roots: Mapping[str, Any], *, write: bool = False, missing: bool = False) -> Path:
    path = _absolute(path)
    configured = {key: _absolute(value) for key, value in roots.items() if key in {"source", "work", "publication"} and value}
    for root in configured.values():
        _no_links(root)
        if not root.is_dir():
            raise MediaError("storage_unavailable", "A configured storage root is unavailable")
    values = list(configured.values())
    if any(_within(a, b) or _within(b, a) for index, a in enumerate(values) for b in values[index + 1:]):
        raise MediaError("unsafe_roots", "Source, work and publication roots must not overlap")
    allowed = [configured[key] for key in (("work", "publication") if write else ("source", "work", "publication")) if key in configured]
    if not any(_within(path, root) and path != root for root in allowed):
        raise MediaError("unsafe_path", "Path is outside the configured permitted roots")
    if write and "source" in configured and _within(path, configured["source"]):
        raise MediaError("source_read_only", "Original collection is read-only")
    _no_links(path, missing=missing)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MediaError("unsafe_path", "Only regular files with one hard link are supported")
        if info.st_size > MAX_AUDIO_BYTES:
            raise MediaError("file_too_large", "File exceeds the configured 8 GiB safety limit")
    elif not missing:
        raise MediaError("missing_file", "Audio file is unavailable")
    return path


@contextlib.contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Walk from filesystem root using pinned no-follow directory handles."""
    if os.name != "posix":
        _no_links(path)
        yield -1
        return
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _file(path: Path) -> Iterator[Any]:
    with _directory(path.parent) as parent:
        fd = os.open(path.name if parent >= 0 else path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent if parent >= 0 else None)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise MediaError("unsafe_path", "File changed into a link or non-regular file")
            yield stream


def sha256_file(path: str | Path) -> str:
    with _file(_absolute(path)) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync_directory(path: Path) -> None:
    with _directory(path) as fd:
        if fd < 0:
            raise MediaError("unsupported_filesystem", "Durable writes require a supported Linux filesystem")
        os.fsync(fd)


def _sync_file(path: Path) -> None:
    with _file(path) as stream:
        os.fsync(stream.fileno())


def _mutation_supported() -> None:
    if sys.platform != "linux":
        raise MediaError("unsupported_filesystem", "Audio mutations require Linux; this development platform is read-only")


def _expect(path: Path, expected: str) -> None:
    if not isinstance(expected, str) or not _SHA.fullmatch(expected):
        raise MediaError("missing_revision", "A full SHA-256 revision is required")
    try:
        actual = sha256_file(path)
    except FileNotFoundError as exc:
        raise MediaError("conflict", "File disappeared after approval") from exc
    if actual != expected:
        raise MediaError("conflict", "File changed after approval")


def _demuxer(path: Path) -> str:
    kind = path.suffix.lower().lstrip(".")
    demuxers = {'mp3':'mp3','flac':'flac','m4a':'mov','m4b':'mov','mp4':'mov','aac':'aac','ogg':'ogg','oga':'ogg','opus':'ogg','wav':'wav','wave':'wav','aif':'aiff','aiff':'aiff','ape':'ape','wv':'wv','wma':'asf','asf':'asf','webm':'matroska','mka':'matroska','mkv':'matroska','dsf':'dsf','dff':'iff','ac3':'ac3','aiff':'aiff'}
    if kind not in demuxers:
        raise MediaError('unsupported_format', 'No safe demuxer profile for this extension')
    return demuxers[kind]


def probe_audio(path: Path) -> dict:
    result = _process(path, 'ffprobe', ['-show_entries','format=duration,format_name:format_tags:stream=codec_type,codec_name,sample_rate,channels,duration','-of','json'], timeout=30)
    doc = json.loads(result.stdout)
    streams = [x for x in doc.get('streams', []) if x.get('codec_type') == 'audio']
    if not streams:
        raise MediaError('no_audio_stream', 'File contains no audio stream')
    return {'duration': float(doc.get('format', {}).get('duration') or streams[0].get('duration') or 0), 'codec': streams[0].get('codec_name'), 'container':doc.get('format', {}).get('format_name'), 'tags': {k.lower():[str(v)] for k,v in doc.get('format', {}).get('tags', {}).items() if k.lower() in _TEXT_FRAMES}}


def fallback_inventory(path, roots, error):
    path = _validate(path, roots)
    info = path.stat()
    result = {'sha256':sha256_file(path),'size':info.st_size,'mtime_ns':info.st_mtime_ns,'format':path.suffix.lower().lstrip('.') or 'unknown','tags':{},'has_art':False,'lyrics':'','lrc':'','write_supported':False,'write_block_reason':error.code}
    try:
        result.update(probe_audio(path))
        result['warnings'] = [{'code':error.code,'message':'Метаданные прочитаны резервным декодером; нативная запись недоступна'}]
    except (MediaError, ValueError, KeyError) as exc:
        result['input_error'] = {'code':getattr(exc,'code','invalid_audio'),'message':getattr(exc,'message','Audio could not be decoded')}
    return result


def _process(path: Path, executable: str, arguments: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    with _file(path) as stream:
        input_path = f"/proc/self/fd/{stream.fileno()}" if sys.platform == "linux" else str(path)
        command = [executable, "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mp3,flac,mov,aac,ogg,wav,aiff,ape,wv,asf,matroska,dsf,iff,ac3", "-i", input_path, *arguments]
        try:
            # Spool child output and enforce a bound while it runs: a malformed or
            # very long file must not consume unbounded Python heap or scratch space.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                with subprocess.Popen(command, stdout=output, stderr=errors,
                                      pass_fds=(stream.fileno(),) if sys.platform == "linux" else ()) as process:
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            process.kill()
                            process.wait()
                            raise MediaError("media_timeout", "Media verification exceeded its time limit")
                        if os.fstat(output.fileno()).st_size > 32 * 1024**2 or os.fstat(errors.fileno()).st_size > 1024**2:
                            process.kill()
                            process.wait()
                            raise MediaError("verification_too_large", "Verification output exceeded its safety limit")
                        time.sleep(0.05)
                    if process.returncode:
                        raise MediaError("invalid_audio", "Media decoding or packet verification failed")
                    if os.fstat(output.fileno()).st_size > 32 * 1024**2 or os.fstat(errors.fileno()).st_size > 1024**2:
                        raise MediaError("verification_too_large", "Verification output exceeded its safety limit")
                    output.seek(0)
                    errors.seek(0)
                    return subprocess.CompletedProcess(command, process.returncode, output.read(), errors.read())
        except FileNotFoundError as exc:
            raise MediaError("tool_unavailable", f"{executable} is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaError("media_timeout", "Media verification exceeded its time limit") from exc
        except subprocess.CalledProcessError as exc:
            raise MediaError("invalid_audio", "Media decoding or packet verification failed") from exc


def audio_identity(path: str | Path) -> str:
    result = _process(Path(path), "ffprobe", ["-select_streams", "a:0", "-show_packets", "-show_streams", "-show_entries", "packet=data_hash:stream=codec_name,sample_rate,channels,extradata_hash", "-show_data_hash", "sha256", "-of", "json"])
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise MediaError("invalid_audio", "Invalid packet verification result") from exc
    packets = payload.get("packets", [])
    if not packets or any(not re.fullmatch(r"SHA256:[0-9a-fA-F]{64}", packet.get("data_hash", "")) for packet in packets):
        raise MediaError("invalid_audio", "No verifiable audio packets found")
    # Side data includes encoder delay/padding. A compact text parser could omit
    # precisely those packets; JSON preserves every selected packet and its data.
    identity = {"packets": [{"hash": packet["data_hash"], "side_data": packet.get("side_data_list", [])} for packet in packets],
                "streams": [{key: stream[key] for key in ("codec_name", "sample_rate", "channels", "extradata_hash") if key in stream} for stream in payload.get("streams", [])]}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def audit_audio(path: str | Path, roots: Mapping[str, Any]) -> dict[str, Any]:
    path = _validate(path, roots)
    digest = sha256_file(path)
    try:
        identity = audio_identity(path)
        _process(path, "ffmpeg", ["-nostdin", "-xerror", "-map", "0:a:0", "-f", "null", "-"], timeout=1800)
        _expect(path, digest)
        return {"state": "passed", "sha256": digest, "audio_identity": identity, "error": None, "warnings": []}
    except MediaError as exc:
        return {"state": "unsupported" if exc.code == "unsupported_format" else "failed", "sha256": digest,
                "error": exc.code, "warnings": [], "message": exc.message}


def inspect_audio(path: str | Path, roots: Mapping[str, Any]) -> dict[str, Any]:
    path = _validate(path, roots)
    with _file(path) as stream:
        info = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        try:
            audio = _load_audio(stream)
        except (mutagen.MutagenError, ValueError, OSError) as exc:
            raise MediaError("invalid_audio", "Audio metadata could not be read") from exc
        if audio is None:
            raise MediaError("unsupported_format", "No supported audio parser found")
        tags: dict[str, Any] = {}
        lyrics = lrc = None
        lyrics_warning = None
        has_art = False
        write_supported = False
        reason = "format_not_validated"
        if isinstance(audio, (MP3, WAVE, AIFF)):
            existing = audio.tags
            for field, frame in _TEXT_FRAMES.items():
                if field=='date' and existing and existing.version[1]==3: frame='TYER'
                if existing and existing.getall(frame):
                    tags[field] = [str(value) for value in existing.getall(frame)[0].text]
            if existing:
                for field, description in _MB_DESCRIPTIONS.items():
                    frames = [frame for frame in existing.getall('TXXX') if frame.desc.casefold()==description.casefold()]
                    if frames:
                        tags[field] = [str(value) for value in frames[0].text]
                if "UFID:http://musicbrainz.org" in existing:
                    tags["musicbrainz_trackid"] = [existing["UFID:http://musicbrainz.org"].data.decode("ascii", errors="replace")]
                has_art = any(frame.type == 3 for frame in existing.getall("APIC"))
                normal, timed = existing.getall("USLT"), existing.getall("SYLT")
                lyrics = normal[0].text if normal else None
                millis = [frame for frame in timed if frame.format == 2 and frame.type == 1]
                if millis:
                    lrc = serialize_lrc([{"text": words, "start_ms": timestamp} for words, timestamp in millis[0].text])
            version = existing.version if existing else (2, 4, 0)
            write_supported = version[1] in {3,4}
            reason = None if write_supported else "id3_version_not_supported"
        elif isinstance(audio, FLAC):
            tags = {key: list(audio.get(key, [])) for key in (*_TEXT_FRAMES, *_CUSTOM_FIELDS) if audio.get(key)}
            has_art = any(picture.type == 3 for picture in audio.pictures)
            raw = audio.get("lyrics", [None])[0]
            lyrics = raw
            if raw and _LRC_TIME.search(raw):
                try:
                    document = parse_lrc(raw)
                    if not document["complete"]:
                        lyrics_warning = "partial_lyrics"
                    elif any(line["start_ms"] > int(audio.info.length * 1000) + 1000 for line in document["lines"]):
                        lyrics_warning = "lyrics_out_of_range"
                    else:
                        # Classification only: preserve the original metadata and
                        # offset declaration without rewriting bytes on inspection.
                        lrc, lyrics = raw, None
                except MediaError as exc:
                    lyrics_warning = exc.code
            write_supported, reason = True, None
        elif isinstance(audio, formats.PROFILES):
            tags, raw, has_art = formats.inspect(audio, (*_TEXT_FRAMES, *_CUSTOM_FIELDS))
            lyrics = raw
            if raw and _LRC_TIME.search(raw):
                try:
                    document = parse_lrc(raw)
                    if document['complete'] and all(x['start_ms'] <= int(audio.info.length*1000)+1000 for x in document['lines']):
                        lrc, lyrics = raw, None
                    else: lyrics_warning = 'partial_or_out_of_range_lyrics'
                except MediaError as exc: lyrics_warning = exc.code
            write_supported, reason = True, None
        else:
            stream.seek(0)
            easy = mutagen.File(stream, easy=True)
            if easy and easy.tags:
                tags = {key: list(value) for key, value in easy.tags.items() if key in _TEXT_FRAMES}
        return {"sha256": digest, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                **intake.describe(path,audio), "duration": float(audio.info.length),
                "tags": tags, "has_art": has_art, "lyrics": lyrics, "lrc": lrc, "lyrics_warning": lyrics_warning,
                "write_supported": write_supported, "write_block_reason": reason,
                "audio_identity": None, "poweramp_verified": False}


def _load_audio(file):
    audio = mutagen.File(file)
    if isinstance(audio, (MP3, WAVE, AIFF)):
        if hasattr(file, 'seek'): file.seek(0)
        if isinstance(audio,(WAVE,AIFF)):
            audio=type(audio)(file,translate=False)
        else:
            try: audio.tags = ID3(file, translate=False)
            except ID3NoHeaderError: audio.tags = None
    return audio


def read_art(path: str | Path, roots: Mapping[str, Any]) -> tuple[bytes, str] | None:
    path = _validate(path, roots)
    with _file(path) as stream:
        audio = _load_audio(stream)
        if isinstance(audio, (MP3, WAVE, AIFF)):
            pictures = audio.tags.getall("APIC") if audio.tags else []
        elif isinstance(audio, FLAC):
            pictures = audio.pictures
        elif isinstance(audio, formats.PROFILES):
            data = formats.front(audio)
            if data:
                mime, _, _ = _cover(data)
                return data, mime
            return None
        else:
            return None
        fronts = [picture for picture in pictures if picture.type == 3]
        if not fronts:
            return None
        data = fronts[0].data
        mime, _, _ = _cover(data)
        return data, mime


def _cover(data: bytes) -> tuple[str, int, int]:
    if not isinstance(data, bytes) or len(data) > MAX_COVER_BYTES:
        raise MediaError("invalid_cover", "Cover exceeds 15 MiB")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format not in {"PNG", "JPEG"} or picture.width * picture.height > 25_000_000:
                raise MediaError("invalid_cover", "Cover must be JPEG/PNG with at most 25 megapixels")
            mime = "image/jpeg" if picture.format == "JPEG" else "image/png"
            width, height = picture.size
            picture.verify()
        return mime, width, height
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise MediaError("invalid_cover", "Cover image is malformed") from exc


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, str) or len(v) > 10000 or "\0" in v for v in values):
        raise MediaError("invalid_tags", "Tag values must be a bounded string or list of strings")
    return values


def _edit(path: Path, tags: Mapping[str, Any], lrc: str | None, cover: bytes | None) -> None:
    if set(tags) - set(_TEXT_FRAMES) - _CUSTOM_FIELDS:
        raise MediaError("invalid_tags", "An unsupported tag field was requested")
    for field in set(tags) & _RG_FIELDS:
        values=_values(tags[field])
        if len(values)>1 or (values and not re.fullmatch(r'[+-]?\d+(?:\.\d+)? dB' if field.endswith('gain') else r'\d+(?:\.\d+)?',values[0])):
            raise MediaError('invalid_tags','Invalid ReplayGain value')
    parsed = parse_lrc(lrc) if lrc is not None else None
    normalized = serialize_lrc(parsed) if parsed else None
    audio = _load_audio(path)
    if audio is None or not isinstance(audio, (MP3, WAVE, AIFF, FLAC, *formats.PROFILES)):
        raise MediaError("unsupported_format", "Only MP3 and FLAC writes are supported")
    if parsed and any(line["start_ms"] > int(audio.info.length * 1000) + 1000 for line in parsed["lines"]):
        raise MediaError("invalid_lyrics", "A lyric timestamp exceeds audio duration")
    cover_info = _cover(cover) if cover is not None else None
    if isinstance(audio, (MP3, WAVE, AIFF)):
        if audio.tags is None:
            audio.add_tags()
        version = audio.tags.version[1]
        if version not in {3,4}:
            raise MediaError('unsupported_id3','Unsupported ID3 version')
        encoding = 1 if version==3 else 3
        for field, value in tags.items():
            values = _values(value)
            if field == "musicbrainz_trackid":
                if len(values) > 1:
                    raise MediaError("invalid_tags", "A recording has one MusicBrainz recording identifier")
                audio.tags.delall("UFID:http://musicbrainz.org")
                if values:
                    try:
                        data = values[0].encode("ascii")
                    except UnicodeEncodeError as exc:
                        raise MediaError("invalid_tags", "MusicBrainz identifiers must contain ASCII characters") from exc
                    audio.tags.add(UFID(owner="http://musicbrainz.org", data=data))
            elif field in _TEXT_FRAMES:
                frame = 'TYER' if field=='date' and version==3 else _TEXT_FRAMES[field]
                if version==3 and field=='date' and any(not re.fullmatch(r'\d{4}',v) for v in values):
                    raise MediaError('invalid_tags','ID3v2.3 date profile accepts a four-digit year')
                audio.tags.delall(frame)
                if values:
                    audio.tags.add(Frames[frame](encoding=encoding, text=values))
            else:
                description = _MB_DESCRIPTIONS[field]
                for frame in list(audio.tags.getall('TXXX')):
                    if frame.desc.casefold()==description.casefold():audio.tags.delall(frame.HashKey)
                if values:
                    audio.tags.add(TXXX(encoding=encoding, desc=description, text=values))
        if parsed is not None:
            # Multiple languages/descriptions are a manual conflict; preserve foreign slots.
            slots = audio.tags.getall("SYLT")
            if len(slots) > 1:
                raise MediaError("lyrics_conflict", "Multiple synchronized lyric slots require an explicit selection")
            desc, lang = (slots[0].desc, slots[0].lang) if slots else ("Phonoteka", "und")
            if slots:
                audio.tags.delall(slots[0].HashKey)
            if parsed["lines"]:
                audio.tags.add(SYLT(encoding=encoding, lang=lang, format=2, type=1, desc=desc,
                                    text=[(line["text"], line["start_ms"]) for line in parsed["lines"]]))
        if cover is not None:
            fronts = [frame for frame in audio.tags.getall("APIC") if frame.type == 3]
            if len(fronts) > 1:
                raise MediaError("cover_conflict", "Multiple front covers require an explicit selection")
            desc = fronts[0].desc if fronts else "Phonoteka front cover"
            if not fronts and any(frame.desc == desc for frame in audio.tags.getall("APIC")):
                raise MediaError("cover_conflict", "A different picture already owns the cover description")
            if fronts:
                audio.tags.delall(fronts[0].HashKey)
            audio.tags.add(APIC(encoding=encoding, mime=cover_info[0], type=3, desc=desc, data=cover))
        audio.save(v2_version=version, v23_sep=None, **({} if isinstance(audio,AIFF) else {'v1':1}))
    elif isinstance(audio, formats.PROFILES):
        try:
            formats.edit(audio, tags, normalized, cover, cover_info, _values)
        except (ValueError, TypeError, OverflowError) as exc:
            raise MediaError('invalid_metadata', 'Invalid metadata or conflicting cover slots') from exc
    else:
        for field, value in tags.items():
            values = _values(value)
            if values:
                audio[field] = values
            elif field in audio:
                del audio[field]
        if normalized is not None:
            if normalized:
                audio["lyrics"] = [normalized]
            elif "lyrics" in audio:
                del audio["lyrics"]
        if cover is not None:
            fronts = [picture for picture in audio.pictures if picture.type == 3]
            if len(fronts) > 1:
                raise MediaError("cover_conflict", "Multiple front covers require an explicit selection")
            others = [picture for picture in audio.pictures if picture.type != 3]
            image = Picture()
            image.type, image.mime, image.width, image.height, image.depth, image.data = 3, cover_info[0], cover_info[1], cover_info[2], 24, cover
            audio.clear_pictures()
            for picture in [*others, image]:
                audio.add_picture(picture)
        audio.save()


def _chunk_signature(path):
    result=[]
    with path.open('rb') as stream:
        header=stream.read(12);big=header[:4]==b'FORM'
        if header[:4] not in {b'RIFF',b'FORM'}: return result
        while chunk:=stream.read(8):
            if len(chunk)!=8: raise MediaError('invalid_audio','Truncated container chunk')
            size=int.from_bytes(chunk[4:],'big' if big else 'little')
            if stream.tell()+size>path.stat().st_size: raise MediaError('invalid_audio','Container chunk exceeds file')
            if chunk[:4].lower() in {b'id3 ',b'data',b'ssnd',b'junk',b'pad '}:
                stream.seek(size+(size%2),1);continue
            digest=hashlib.sha256();left=size
            while left:
                data=stream.read(min(left,1024*1024))
                if not data: raise MediaError('invalid_audio','Truncated container chunk')
                digest.update(data);left-=len(data)
            result.append([chunk[:4].hex(),digest.hexdigest()])
            if size%2:stream.seek(1,1)
    return result


def _untouched_signature(path: Path, tags: Mapping[str, Any], lrc: str | None, cover: bytes | None) -> dict[str, Any]:
    """Verify untouched metadata as well as codec packets before replacing target."""
    with _file(path) as stream:
        audio = _load_audio(stream)
        if isinstance(audio, (MP3, WAVE, AIFF)):
            excluded = {('TYER' if key=='date' and audio.tags and audio.tags.version[1]==3 else _TEXT_FRAMES[key]) for key in tags if key in _TEXT_FRAMES}
            excluded.update(f"TXXX:{_MB_DESCRIPTIONS[key]}" for key in tags if key in _CUSTOM_FIELDS and key != "musicbrainz_trackid")
            if "musicbrainz_trackid" in tags:
                excluded.add("UFID:http://musicbrainz.org")
            if not audio.tags:
                return {"frames": {}, "unknown": [], **({"chunks":_chunk_signature(path)} if isinstance(audio,(WAVE,AIFF)) else {})}
            frames = {key: repr(frame) for key, frame in audio.tags.items()
                      if key.casefold() not in {x.casefold() for x in excluded} and not (lrc is not None and key.startswith("SYLT:"))
                      and not (cover is not None and key.startswith("APIC:") and frame.type == 3)}
            return {"frames": frames, "unknown": [hashlib.sha256(raw).hexdigest() for raw in audio.tags.unknown_frames], **({"chunks":_chunk_signature(path)} if isinstance(audio,(WAVE,AIFF)) else {})}
        if isinstance(audio, formats.PROFILES):
            return formats.untouched(audio, tags, lrc, cover)
        if isinstance(audio, FLAC):
            excluded = {key.lower() for key in tags}
            if lrc is not None:
                excluded.add("lyrics")
            fields = {key.lower(): list(value) for key, value in audio.items() if key.lower() not in excluded}
            pictures = [hashlib.sha256(picture.write()).hexdigest() for picture in audio.pictures if cover is None or picture.type != 3]
            blocks = [(type(block).__name__, hashlib.sha256(block.write()).hexdigest())
                      for block in audio.metadata_blocks if type(block).__name__ not in {"VCFLACDict", "Picture", "Padding"}]
            return {"fields": fields, "pictures": pictures, "blocks": blocks,
                    "vendor": audio.tags.vendor if audio.tags else None}
    raise MediaError("unsupported_format", "No preservation profile for this format")


def _make_directory(path: Path) -> None:
    _no_links(path, missing=True)
    if path.exists():
        if not path.is_dir():
            raise MediaError("unsafe_path", "Expected a directory")
        return
    _make_directory(path.parent)
    with _directory(path.parent) as parent:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        os.fsync(parent)
    _no_links(path)


def _copy(source: Path, destination: Path) -> None:
    with _file(source) as stream, _directory(destination.parent) as parent:
        fd = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as output:
            shutil.copyfileobj(stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    _sync_directory(destination.parent)


def _preserve_access(source: Path, target: Path) -> None:
    """Preserve POSIX ownership, mode and xattrs (including POSIX ACLs), fail closed."""
    with _file(source) as before, _file(target) as after:
        info = os.fstat(before.fileno())
        current = os.fstat(after.fileno())
        if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
            os.fchown(after.fileno(), info.st_uid, info.st_gid)
        os.fchmod(after.fileno(), stat.S_IMODE(info.st_mode))
        source_attrs = {key: os.getxattr(before.fileno(), key) for key in os.listxattr(before.fileno())}
        for key in os.listxattr(after.fileno()):
            if key not in source_attrs:
                os.removexattr(after.fileno(), key)
        for key, value in source_attrs.items():
            os.setxattr(after.fileno(), key, value)
        if any(os.getxattr(after.fileno(), key) != value for key, value in source_attrs.items()):
            raise MediaError("permissions_not_preserved", "Extended attributes did not survive staging")
        os.fsync(after.fileno())


def _replace(source: Path, target: Path, *, no_replace: bool = False) -> None:
    with _directory(source.parent) as source_dir, _directory(target.parent) as target_dir:
        if no_replace:
            libc = ctypes.CDLL(None, use_errno=True)
            function = getattr(libc, "renameat2", None)
            if function is None:
                raise MediaError("unsupported_filesystem", "Atomic no-replace rename is unavailable")
            function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
            if function(source_dir, os.fsencode(source.name), target_dir, os.fsencode(target.name), 1) != 0:
                error = ctypes.get_errno()
                if error == errno.EEXIST:
                    raise MediaError("conflict", "Destination appeared after approval")
                if error in {errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise MediaError("unsupported_filesystem", "Atomic no-replace publication is not supported by these mounts")
                raise OSError(error, os.strerror(error))
        else:
            os.replace(source.name, target.name, src_dir_fd=source_dir, dst_dir_fd=target_dir)


def _journal(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with _directory(path.parent) as parent:
        fd = os.open(temporary.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(document, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
    _replace(temporary, path)
    _sync_directory(path.parent)


def _transaction(target: Path, staged: Path, *, before: str | None, backup_dir: Path,
                 roots: Mapping[str, Any], authorize: Callable[[], None] | None = None,
                 source_backup: Path | None = None, action: str = "apply") -> dict[str, Any]:
    operation_id = uuid.uuid4().hex
    if any(_within(backup_dir, _absolute(value)) or _within(_absolute(value), backup_dir)
           for key, value in roots.items() if key in {"source", "work", "publication"} and value):
        raise MediaError("unsafe_roots", "Backups must be outside source/work/publication")
    _make_directory(backup_dir)
    backup = backup_dir / f"{operation_id}{target.suffix}.bak" if before else None
    if before:
        _expect(target, before)
        _copy(target, backup)
        _expect(backup, before)
        _preserve_access(target, backup)
    after = sha256_file(staged)
    _sync_file(staged)
    journal_path = backup_dir / f"{operation_id}.json"
    document = {"schema": 1, "operation_id": operation_id, "action": action,
                "operation": "replace" if before else "create", "state": "prepared",
                "target": str(target), "staged": str(staged), "backup": str(backup) if backup else None,
                "before_sha256": before, "after_sha256": after, "created_at": time.time()}
    _journal(journal_path, document)
    try:
        _validate(target, roots, write=True, missing=before is None)
        if authorize:
            authorize()
        if before:
            _expect(target, before)
        document["state"] = "replacing"
        _journal(journal_path, document)
        _replace(staged, target, no_replace=before is None)
        _sync_file(target)
        _sync_directory(target.parent)
        if staged.parent != target.parent:
            _sync_directory(staged.parent)
        _expect(target, after)
        document["state"] = "committed"
        _journal(journal_path, document)
    except Exception:
        # Do not delete stage/backup/journal: recovery needs all three if sync failed.
        raise
    return {"operation_id": operation_id, "sha256": after, "after_sha256": after, "before_sha256": before,
            "backup_path": str(backup) if backup else None, "journal_path": str(journal_path), "state": "committed"}


def _stage(target: Path, source: Path, directory: Path | None = None) -> Path:
    directory = directory or target.parent / ".phonoteka-staging"
    _make_directory(directory)
    staged = directory / f"{uuid.uuid4().hex}{target.suffix}"
    _copy(source, staged)
    return staged


def _staging_directory(target: Path, roots: Mapping[str, Any]) -> Path | None:
    publication = roots.get("publication")
    if publication and _within(target, _absolute(publication)):
        return _absolute(publication) / ".staging"
    return None


def _apply_changes_impl(path: str | Path, tags: Mapping[str, Any], lrc: str | None,
                  expected_sha256: str, backup_dir: str | Path, roots: Mapping[str, Any],
                  cover: bytes | None = None, *, authorize: Callable[[], None] | None = None,
                  full_decode: bool = True) -> dict[str, Any]:
    _mutation_supported()
    path = _validate(path, roots, write=True)
    if "work" not in roots or not _within(path, _absolute(roots["work"])):
        raise MediaError("unsafe_path", "Editing is permitted only in the work tree")
    _expect(path, expected_sha256)
    metadata = inspect_audio(path, roots)
    if not metadata["write_supported"]:
        raise MediaError("unsupported_format", str(metadata["write_block_reason"]))
    before_audio = audio_identity(path)
    untouched = _untouched_signature(path, tags, lrc, cover)
    staged = _stage(path, path)
    _edit(staged, tags, lrc, cover)
    if audio_identity(staged) != before_audio:
        raise MediaError("audio_changed", "Tag staging changed the encoded audio packets")
    if _untouched_signature(staged, tags, lrc, cover) != untouched:
        raise MediaError("metadata_changed", "Tag staging changed unrelated metadata")
    checked = inspect_audio(staged, roots)
    for key, value in tags.items():
        if checked["tags"].get(key, []) != _values(value):
            raise MediaError("verification_failed", "A requested tag did not survive independent reopening")
    if lrc is not None and checked["lrc"] != serialize_lrc(parse_lrc(lrc)) and lrc.strip():
        raise MediaError("verification_failed", "Lyrics did not survive independent reopening")
    if cover is not None and read_art(staged, roots) != (cover, _cover(cover)[0]):
        raise MediaError("verification_failed", "Cover did not survive independent reopening")
    if full_decode:
        _process(staged, "ffmpeg", ["-nostdin", "-xerror", "-map", "0:a:0", "-f", "null", "-"], timeout=1800)
    _preserve_access(path, staged)
    result = _transaction(path, staged, before=expected_sha256, backup_dir=_absolute(backup_dir), roots=roots, authorize=authorize)
    result["audio_identity"] = before_audio
    return result


def _rollback_impl(path: str | Path, backup_path: str | Path, expected_sha256: str,
             roots: Mapping[str, Any], *, backup_sha256: str | None = None,
             backup_dir: str | Path | None = None,
             authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    _mutation_supported()
    path = _validate(path, roots, write=True)
    backup = _absolute(backup_path)
    _no_links(backup)
    _expect(path, expected_sha256)
    if backup_sha256 is None:
        journal_path = backup.with_name(backup.name.split(".", 1)[0] + ".json")
        with _file(journal_path) as stream:
            document = json.load(stream)
        if document.get("backup") != str(backup) or document.get("target") != str(path):
            raise MediaError("invalid_backup", "Backup does not belong to this target")
        backup_sha256 = document["before_sha256"]
    _expect(backup, backup_sha256)
    staged = _stage(path, backup, _staging_directory(path, roots))
    _expect(staged, backup_sha256)
    _preserve_access(backup, staged)
    return _transaction(path, staged, before=expected_sha256, backup_dir=_absolute(backup_dir) if backup_dir else backup.parent,
                        roots=roots, authorize=authorize, action="rollback")


def _publish_impl(source: str | Path, destination: str | Path, expected_source_sha256: str,
            roots: Mapping[str, Any], publication_root: str | Path,
            expected_destination_sha256: str | None = None, *, backup_dir: str | Path | None = None,
            authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    _mutation_supported()
    source = _validate(source, roots)
    publication = _absolute(publication_root)
    if publication != _absolute(roots["publication"]):
        raise MediaError("unsafe_roots", "Publication root differs from configured root")
    destination = _absolute(destination)
    if not _within(destination, publication / "Ready") or destination == publication / "Ready":
        raise MediaError("unsafe_path", "Publication target must be within publication/Ready")
    if not _within(source, _absolute(roots["work"])):
        raise MediaError("unsafe_path", "Publication requires an accepted work revision")
    if backup_dir is None:
        raise MediaError("missing_backup_root", "Publication requires a configured backup directory")
    _expect(source, expected_source_sha256)
    _validate(destination, roots, write=True, missing=True)
    if expected_destination_sha256:
        _expect(destination, expected_destination_sha256)
    elif destination.exists():
        raise MediaError("conflict", "Destination already exists; an explicit destination revision is required")
    _make_directory(destination.parent)
    staged = _stage(destination, source, publication / ".staging")
    _expect(staged, expected_source_sha256)
    _expect(source, expected_source_sha256)
    _preserve_access(source, staged)
    return _transaction(destination, staged, before=expected_destination_sha256,
                        backup_dir=_absolute(backup_dir), roots=roots, authorize=authorize, action="publish")


def _existing_parent(path: Path) -> Path:
    while not path.exists():
        if path == path.parent:
            raise MediaError("storage_unavailable", "Storage parent is unavailable")
        path = path.parent
    _no_links(path)
    return path if path.is_dir() else path.parent


@contextlib.contextmanager
def _reserve_filesystem_space(requests: list[tuple[Path, int]]) -> Iterator[None]:
    """One guarded worker process; all its I/O threads share atomic reservations.

    st_dev groups bind aliases of one filesystem. External writers can still fill
    storage after preflight; ENOSPC leaves the backup and journal intact.
    """
    grouped: dict[int, tuple[Path, int]] = {}
    for directory, size in requests:
        existing = _existing_parent(directory)
        device = existing.stat().st_dev
        old = grouped.get(device, (existing, 0))
        grouped[device] = (old[0], old[1] + size)
    with _SPACE_LOCK:
        for device, (directory, size) in grouped.items():
            free = shutil.disk_usage(directory).free
            if free - _SPACE_RESERVED.get(device, 0) - size < _SPACE_MARGIN:
                raise MediaError("insufficient_space", "Not enough unreserved space for staging, backup and safety margin", retryable=True)
        for device, (_, size) in grouped.items():
            _SPACE_RESERVED[device] = _SPACE_RESERVED.get(device, 0) + size
    try:
        yield
    finally:
        with _SPACE_LOCK:
            for device, (_, size) in grouped.items():
                remainder = _SPACE_RESERVED[device] - size
                if remainder:
                    _SPACE_RESERVED[device] = remainder
                else:
                    del _SPACE_RESERVED[device]


def apply_changes(path: str | Path, tags: Mapping[str, Any], lrc: str | None,
                  expected_sha256: str, backup_dir: str | Path, roots: Mapping[str, Any],
                  cover: bytes | None = None, *, authorize: Callable[[], None] | None = None,
                  full_decode: bool = True) -> dict[str, Any]:
    _mutation_supported()
    target = _validate(path, roots, write=True)
    size = target.stat().st_size
    with _reserve_filesystem_space([(target.parent, size + _TAG_GROWTH_BUDGET), (_absolute(backup_dir), size)]):
        return _apply_changes_impl(path, tags, lrc, expected_sha256, backup_dir, roots, cover,
                                   authorize=authorize, full_decode=full_decode)


def rollback(path: str | Path, backup_path: str | Path, expected_sha256: str,
             roots: Mapping[str, Any], *, backup_sha256: str | None = None,
             backup_dir: str | Path | None = None,
             authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    _mutation_supported()
    target = _validate(path, roots, write=True)
    backup = _absolute(backup_path)
    _no_links(backup)
    destination_backups = _absolute(backup_dir) if backup_dir else backup.parent
    stage = _staging_directory(target, roots) or target.parent
    with _reserve_filesystem_space([(stage, backup.stat().st_size), (destination_backups, target.stat().st_size)]):
        return _rollback_impl(path, backup_path, expected_sha256, roots, backup_sha256=backup_sha256,
                              backup_dir=backup_dir, authorize=authorize)


def publish(source: str | Path, destination: str | Path, expected_source_sha256: str,
            roots: Mapping[str, Any], publication_root: str | Path,
            expected_destination_sha256: str | None = None, *, backup_dir: str | Path | None = None,
            authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    _mutation_supported()
    source_path = _validate(source, roots)
    target = _validate(destination, roots, write=True, missing=True)
    if backup_dir is None:
        raise MediaError("missing_backup_root", "Publication requires a configured backup directory")
    requests = [(_absolute(publication_root), source_path.stat().st_size + 1024 * 1024)]
    if target.exists():
        requests.append((_absolute(backup_dir), target.stat().st_size))
    with _reserve_filesystem_space(requests):
        return _publish_impl(source, destination, expected_source_sha256, roots, publication_root,
                             expected_destination_sha256, backup_dir=backup_dir, authorize=authorize)


def recover(journal_path: str | Path, roots: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile only. Never resume a prepared write without a new authorization."""
    _mutation_supported()
    journal_path = _absolute(journal_path)
    with _file(journal_path) as stream:
        if os.fstat(stream.fileno()).st_size > 65536:
            raise MediaError("invalid_journal", "Journal exceeds size limit")
        document = json.load(stream)
    if document.get("schema") != 1 or document.get("operation") not in {"replace", "create"}:
        raise MediaError("invalid_journal", "Unknown journal schema")
    target = _validate(document["target"], roots, write=True, missing=True)
    actual = sha256_file(target) if target.exists() else None
    if actual == document["after_sha256"]:
        # Even after hash equality, fsync failure keeps the operation interrupted.
        _sync_file(target)
        _sync_directory(target.parent)
        staged = _absolute(document["staged"])
        _no_links(staged.parent)
        _sync_directory(staged.parent)
        document["state"] = "committed"
    elif actual == document.get("before_sha256"):
        document["state"] = "interrupted"
        document["reason"] = "new_authorization_required"
    elif actual is None and document["operation"] == "replace":
        backup = _absolute(document["backup"])
        _expect(backup, document["before_sha256"])
        staged = _stage(target, backup, _staging_directory(target, roots))
        _preserve_access(backup, staged)
        _replace(staged, target, no_replace=True)
        _sync_file(target)
        _sync_directory(target.parent)
        _sync_directory(staged.parent)
        document["state"] = "interrupted"
        document["reason"] = "before_restored_new_authorization_required"
    else:
        document["state"] = "conflict"
        document["reason"] = "target_has_unexpected_revision"
    _journal(journal_path, document)
    return document


@contextlib.contextmanager
def writer_guard(directory: str | Path, owner: str = "phonoteka") -> Iterator[None]:
    """Storage-domain lock, shared by every deployment that can write these trees."""
    _mutation_supported()
    import fcntl
    directory = _absolute(directory)
    _no_links(directory)
    with _directory(directory) as parent:
        fd = os.open("writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            if os.fstat(fd).st_nlink != 1:
                raise MediaError("unsafe_path", "Storage guard must not be a hard link")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MediaError("storage_in_use", "Another worker owns this storage domain") from exc
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"owner": owner, "pid": os.getpid(), "started_at": time.time()}).encode())
            os.fsync(fd)
            os.fsync(parent)
            yield
        finally:
            os.close(fd)


# Concise aliases for worker integrations.
inspect = inspect_audio
apply = apply_changes
