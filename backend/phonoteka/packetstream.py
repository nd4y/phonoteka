"""Bounded ffprobe packet JSON ingestion with legacy-compatible identities."""
from __future__ import annotations

import codecs
import contextlib
import hashlib
import json
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
from typing import Callable, Iterable, Iterator

CHUNK_BYTES = 64 * 1024
VALUE_BYTES = 1024 * 1024
MAX_STREAMS = 64
STDERR_BYTES = 1024 * 1024
_MISSING = object()
_FIELDS = ('codec_name', 'sample_rate', 'channels', 'extradata_hash')


class PacketStreamError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _invalid() -> PacketStreamError:
    return PacketStreamError('invalid_audio', 'Invalid or incomplete packet verification result')


def _large() -> PacketStreamError:
    return PacketStreamError('verification_too_large', 'A verification record exceeded its safety limit')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _constant(_value):
    raise _invalid()


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise _invalid()
    return result


def _decoder():
    return json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)


def _streams(value):
    if not isinstance(value, list) or not value:
        raise _invalid()
    if len(value) > MAX_STREAMS:
        raise _large()
    seen = set()
    for stream in value:
        if not isinstance(stream, dict) or type(stream.get('index')) is not int or stream['index'] < 0:
            raise _invalid()
        if stream['index'] in seen:
            raise _invalid()
        seen.add(stream['index'])
    return [(stream['index'], {key: stream[key] for key in _FIELDS if key in stream}) for stream in value]


def stream_header(raw: bytes):
    if len(raw) > VALUE_BYTES:
        raise _large()
    try:
        decoder = _decoder()
        text = raw.decode('utf-8')
        value, end = decoder.raw_decode(text, len(text)-len(text.lstrip()))
        if text[end:].strip() or not isinstance(value, dict):
            raise _invalid()
        return _streams(value.get('streams'))
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise _invalid() from exc


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise _invalid() from exc


class PacketIdentity:
    """Keep one bounded JSON value and a digest, never an entire packet array."""

    def __init__(self, streams):
        self.streams = streams
        self.ordinals = {index: ordinal for ordinal, (index, _value) in enumerate(streams)}
        if not self.ordinals or len(self.ordinals) != len(streams):
            raise _invalid()
        self.multiple = len(streams) > 1
        self.hash = hashlib.sha256(b'{"packets":[')
        self.observed = set()
        self.count = 0
        self.buffer = ''
        self.position = 0
        self.utf8 = codecs.getincrementaldecoder('utf-8')('strict')
        self.decoder = _decoder()
        self.state = 'open'
        self.key = ''
        self.keys = set()
        self.first = True
        self.eof = False
        self.metadata_bytes = 0
        self.finished = False

    def _space(self):
        while self.position < len(self.buffer) and self.buffer[self.position] in ' \r\n\t':
            self.position += 1

    def _value(self, maximum, delimiters):
        try:
            value, end = self.decoder.raw_decode(self.buffer, self.position)
        except json.JSONDecodeError:
            if len(self.buffer[self.position:].encode('utf-8')) > maximum:
                raise _large()
            if self.eof:
                raise _invalid()
            return _MISSING
        if len(self.buffer[self.position:end].encode('utf-8')) > maximum:
            raise _large()
        following = end
        while following < len(self.buffer) and self.buffer[following] in ' \r\n\t':
            following += 1
        if following == len(self.buffer) and not self.eof:
            return _MISSING
        if following >= len(self.buffer) or self.buffer[following] not in delimiters:
            # raw_decode can accept the integer prefix of an exponent whose
            # remaining bytes have not arrived yet. Never commit that prefix.
            if not self.eof and type(value) in (int, float) and re.fullmatch(
                    r'[-+0-9.eE]+', self.buffer[self.position:]):
                return _MISSING
            raise _invalid()
        self.position = end
        return value

    def _packet(self, packet):
        if not isinstance(packet, dict) or not isinstance(packet.get('data_hash'), str) or not re.fullmatch(
                r'SHA256:[0-9a-fA-F]{64}', packet['data_hash']):
            raise PacketStreamError('invalid_audio', 'No verifiable audio packets found')
        index = packet.get('stream_index')
        if type(index) is not int or index not in self.ordinals:
            raise _invalid()
        value = {'hash': packet['data_hash'], 'side_data': packet.get('side_data_list', [])}
        if self.multiple:
            value['stream'] = self.ordinals[index]
        if self.count:
            self.hash.update(b',')
        self.hash.update(_canonical(value))
        self.count += 1
        self.observed.add(index)

    def _step(self):
        self._space()
        if self.position == len(self.buffer):
            return False
        char = self.buffer[self.position]
        if self.state == 'open':
            if char != '{':
                raise _invalid()
            self.position += 1; self.state = 'key'; self.first = True
        elif self.state == 'key':
            if char == '}' and self.first:
                self.position += 1; self.state = 'done'
            else:
                key = self._value(256, ':')
                if key is _MISSING:
                    return False
                if not isinstance(key, str) or key in self.keys or len(self.keys) >= 32:
                    raise _invalid()
                self.key = key; self.keys.add(key); self.state = 'colon'
        elif self.state == 'colon':
            if char != ':':
                raise _invalid()
            self.position += 1; self.state = 'value'
        elif self.state == 'value':
            if self.key == 'packets':
                if char != '[':
                    raise _invalid()
                self.position += 1; self.state = 'packet'; self.first = True
            else:
                before = self.position
                value = self._value(VALUE_BYTES, ',}')
                if value is _MISSING:
                    return False
                self.metadata_bytes += len(self.buffer[before:self.position].encode('utf-8'))
                if self.metadata_bytes > VALUE_BYTES:
                    raise _large()
                if self.key == 'streams' and _canonical(_streams(value)) != _canonical(self.streams):
                    raise _invalid()
                self.state = 'root_separator'
        elif self.state == 'packet':
            if char == ']' and self.first:
                self.position += 1; self.state = 'root_separator'
            else:
                if char != '{':
                    raise _invalid()
                packet = self._value(VALUE_BYTES, ',]')
                if packet is _MISSING:
                    return False
                self._packet(packet); self.state = 'packet_separator'
        elif self.state == 'packet_separator':
            if char == ',':
                self.position += 1; self.state = 'packet'; self.first = False
            elif char == ']':
                self.position += 1; self.state = 'root_separator'
            else:
                raise _invalid()
        elif self.state == 'root_separator':
            if char == ',':
                self.position += 1; self.state = 'key'; self.first = False
            elif char == '}':
                self.position += 1; self.state = 'done'
            else:
                raise _invalid()
        elif self.state == 'done':
            raise _invalid()
        return True

    def feed(self, raw: bytes, *, final=False):
        if self.finished:
            raise _invalid()
        if len(raw) > CHUNK_BYTES:
            raise _large()
        try:
            self.buffer += self.utf8.decode(raw, final=final)
            self.eof = final
            while self._step():
                if self.position >= CHUNK_BYTES:
                    self.buffer = self.buffer[self.position:]; self.position = 0
            # A pending record/key can grow only to its own budget. Whitespace
            # has already been consumed and never accumulates across reads.
            if len(self.buffer[self.position:].encode('utf-8')) > VALUE_BYTES:
                raise _large()
            self.buffer = self.buffer[self.position:]; self.position = 0
        except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            raise _invalid() from exc

    def finish(self):
        self.feed(b'', final=True)
        if self.state != 'done' or self.buffer.strip() or not {'packets', 'streams'} <= self.keys:
            raise _invalid()
        if not self.count or self.observed != set(self.ordinals):
            raise PacketStreamError('invalid_audio', 'Not every declared audio stream has verifiable packets')
        self.hash.update(b']')
        if self.multiple:
            self.hash.update(b',"scheme":"encoded_audio_packets_v2_all_streams"')
        self.hash.update(b',"streams":')
        self.hash.update(_canonical([value for _index, value in self.streams]))
        self.hash.update(b'}')
        self.finished = True
        return self.hash.hexdigest()


def identity(chunks: Iterable[bytes], streams):
    parser = PacketIdentity(streams)
    for chunk in chunks:
        parser.feed(chunk)
    return parser.finish()


@contextlib.contextmanager
def process_chunks(command: list[str], *, pass_fds=(), timeout=300,
                   cancelled: Callable[[], bool] | None = None) -> Iterator[Iterator[bytes]]:
    """Drain both pipes through four fixed chunks and reap on every exit path."""
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 pass_fds=pass_fds, start_new_session=os.name == 'posix')
    except FileNotFoundError as exc:
        raise PacketStreamError('tool_unavailable', 'ffprobe is not installed') from exc
    stop = threading.Event()
    events = queue.Queue(maxsize=4)
    deadline = time.monotonic()+timeout

    def send(item):
        while not stop.is_set():
            try:
                events.put(item, timeout=0.05)
                return
            except queue.Full:
                pass

    def read(pipe, kind):
        try:
            while not stop.is_set():
                raw = pipe.read1(CHUNK_BYTES)
                if not raw:
                    break
                send((kind, raw))
        except (OSError, ValueError):
            if not stop.is_set():
                send(('error', None))
        finally:
            send((kind, None))

    readers = [threading.Thread(target=read, args=(pipe, kind), daemon=True)
               for pipe, kind in ((child.stdout, 'stdout'), (child.stderr, 'stderr'))]
    completed = False

    def chunks():
        nonlocal completed
        closed = set(); errors = 0
        while len(closed) < 2:
            if cancelled and cancelled():
                raise PacketStreamError('media_cancelled', 'Media verification was cancelled')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise PacketStreamError('media_timeout', 'Media verification exceeded its time limit')
            try:
                kind, raw = events.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if kind == 'error':
                raise _invalid()
            if raw is None:
                closed.add(kind)
            elif kind == 'stderr':
                errors += len(raw)
                if errors > STDERR_BYTES:
                    raise PacketStreamError('verification_too_large', 'Verification diagnostics exceeded their safety limit')
            else:
                yield raw
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise PacketStreamError('media_timeout', 'Media verification exceeded its time limit')
        try:
            code = child.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise PacketStreamError('media_timeout', 'Media verification exceeded its time limit') from exc
        completed = True
        if code:
            raise PacketStreamError('invalid_audio', 'Media decoding or packet verification failed')

    try:
        for reader in readers:
            reader.start()
        yield chunks()
    finally:
        stop.set()
        if not completed:
            if os.name == 'posix':
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
            elif child.poll() is None:
                child.kill()
        cleanup_timeout = False
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cleanup_timeout = True
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=5)
        if cleanup_timeout or any(reader.is_alive() for reader in readers):
            raise PacketStreamError('media_timeout', 'Verification process cleanup could not be confirmed')
        child.stdout.close(); child.stderr.close()
