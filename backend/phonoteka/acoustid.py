"""Owner-configured AcoustID lookup. Only Chromaprint, duration and client key leave the host."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid
import wave

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from . import db, media, provider_attempts, provider_policy, security
from .providers import ProviderError, Providers

PROFILE = 'chromaprint-fpcalc-120s-v1'
ENDPOINT = 'https://api.acoustid.org/v2/lookup'
KEY = re.compile(r'[A-Za-z0-9]{8,128}')
UUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')


@contextmanager
def connection_file(settings):
    """One lock for app/worker, no symlinks, atomic replacement and private permissions."""
    import fcntl
    root = settings.state_dir / 'integrations'
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise media.MediaError('invalid_configuration', 'Недопустимый каталог подключений')
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise media.MediaError('invalid_configuration', 'Каталог подключений должен принадлежать приложению и иметь права 0700')
    lock = os.open(root / 'acoustid.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise media.MediaError('invalid_configuration', 'Небезопасный файл блокировки подключения')
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield root / 'acoustid.json'
    finally:
        os.close(lock)


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {'enabled': False, 'key': '', 'version': 'initial'}
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096 or stat.S_IMODE(info.st_mode) & 0o077:
            raise media.MediaError('invalid_configuration', 'Небезопасный файл подключения AcoustID')
        value = json.load(stream)
    if (not isinstance(value, dict) or type(value.get('enabled')) is not bool
            or not isinstance(value.get('key'), str) or value['key'] and not KEY.fullmatch(value['key'])
            or not isinstance(value.get('version'), str)):
        raise media.MediaError('invalid_configuration', 'Некорректное подключение AcoustID')
    return value


def connection(settings):
    with connection_file(settings) as path:
        return _read(path)


def public_connection(settings, value=None):
    value = connection(settings) if value is None else value
    return {'enabled': value['enabled'], 'key_set': bool(value['key']), 'version': value['version'],
            'transmission_allowed': settings.external_fingerprints,
            'configured': bool(settings.external_fingerprints and value['enabled'] and value['key'])}


def save_connection(settings, version, enabled, key=None):
    if key is not None and key and not KEY.fullmatch(key):
        raise security.error(422, 'invalid_key', 'Нужен Application API key AcoustID: 8-128 латинских букв и цифр')
    with connection_file(settings) as path:
        current = _read(path)
        if current['version'] != version:
            raise security.error(409, 'connection_conflict', 'Подключение изменилось. Обновите настройки перед сохранением.')
        value = {'enabled': enabled, 'key': current['key'] if key is None else key, 'version': uuid.uuid4().hex}
        if enabled and not value['key']:
            raise security.error(422, 'key_required', 'Укажите Application API key AcoustID')
        fd, name = tempfile.mkstemp(prefix='.acoustid-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
    return public_connection(settings, value)


def fingerprint(path, roots):
    path = media._validate(path, roots)
    # Decode through the existing protocol/demuxer restrictions; fpcalc only
    # sees a bounded, locally generated WAV, never an input playlist or URL.
    audio = media._process(path, 'ffmpeg', ['-nostdin', '-threads', '1', '-t', '120', '-map', '0:a:0',
        '-vn', '-sn', '-dn', '-ac', '1', '-ar', '11025', '-f', 'wav', 'pipe:1'], timeout=90, stdout_limit=3*1024**2).stdout
    with tempfile.TemporaryDirectory(prefix='phonoteka-chromaprint-') as directory:
        sample = Path(directory) / 'sample.wav'; sample.write_bytes(audio)
        try:
            result = subprocess.run(['fpcalc', '-json', '-length', '120', str(sample)], capture_output=True, timeout=30)
        except FileNotFoundError as exc:
            raise media.MediaError('tool_unavailable', 'Chromaprint не установлен') from exc
        except subprocess.TimeoutExpired as exc:
            raise media.MediaError('media_timeout', 'Истекло время вычисления Chromaprint') from exc
    try:
        if len(result.stdout) > 65536: raise ValueError()
        data = json.loads(result.stdout)
        value = data['fingerprint']; seconds = float(data['duration'])
        if (result.returncode not in (0, 3) or not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,20000}', value)
                or not math.isfinite(seconds) or not 3 <= seconds <= 121): raise ValueError()
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise media.MediaError('fingerprint_failed', 'Не удалось вычислить Chromaprint') from exc


async def lookup(providers, signature, duration, authorize=lambda: None, *, probe=False):
    with providers.metrics.provider('acoustid', 'metadata' if probe else 'fingerprint'):
        return await _lookup(providers, signature, duration, authorize, probe=probe)


async def _lookup(providers, signature, duration, authorize=lambda: None, *, probe=False):
    settings = providers.settings
    cfg = await asyncio.to_thread(connection, settings)
    if not settings.external_fingerprints or not cfg['enabled']:
        raise ProviderError('provider_disabled', 'acoustid', 'AcoustID выключен или передача отпечатков запрещена')
    if not cfg['key']: raise ProviderError('key_required', 'acoustid', 'Нужен Application API key AcoustID')
    def current_authorization():
        authorize()
        if connection(settings)['version'] != cfg['version']:
            raise media.MediaError('connection_conflict', 'Подключение AcoustID изменилось; запрос не отправлен')
    def admit():
        # App connection checks and worker lookups share one admission clock.
        while True:
            current_authorization()
            with db.transaction(settings.db_path) as conn:
                row = conn.execute("SELECT value FROM settings WHERE key='acoustid_next_request'").fetchone()
                now = time.time(); delay = float(row[0])-now if row else 0
                if delay <= 0:
                    conn.execute("INSERT OR REPLACE INTO settings VALUES('acoustid_next_request',?)", (str(now+.4),))
                    return
            time.sleep(min(delay, .1))
    if (not isinstance(signature, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,20000}', signature)
            or not math.isfinite(duration) or not 3 <= duration <= 21600):
        raise ProviderError('invalid_fingerprint', 'acoustid', 'Некорректный отпечаток или длительность')
    usage = await asyncio.to_thread(provider_policy.check, settings, 'acoustid', 'metadata', 'fetch')
    cache_key = hashlib.sha256(db.encode([PROFILE, usage['policy_version'], signature, round(duration)]).encode()).hexdigest()
    async with providers.locks.setdefault('acoustid-flight', asyncio.Lock()):
        current_authorization()
        cached = await asyncio.to_thread(db.fetchone, settings.db_path,
            'SELECT payload FROM provider_cache WHERE key=? AND expires>?', (cache_key, time.time())) if usage['allow_cache'] and not probe else None
        if cached: return json.loads(cached['payload'])
        breaker = json.loads(await asyncio.to_thread(db.setting, settings.db_path, 'provider_breaker:acoustid', '{}'))
        if breaker.get('until', 0) > time.time():
            raise ProviderError('provider_cooldown', 'acoustid', 'AcoustID временно приостановлен')
        with provider_attempts.request_scope() as attempt_state:
            try:
                async with asyncio.timeout(45):
                    # No credentials, fingerprint or file names in URL/telemetry.
                    async with providers.request('acoustid', 'metadata' if probe else 'fingerprint', 'POST', ENDPOINT,
                            authorize=admit, data={'client': cfg['key'], 'fingerprint': signature,
                                'duration': str(round(duration)), 'meta': 'recordings', 'format': 'json'}) as response:
                        if response.status_code == 429:
                            await asyncio.to_thread(db.set_setting, settings.db_path, 'provider_breaker:acoustid',
                                db.encode({'until': time.time()+300, 'reason': 'rate_limited'}))
                            raise ProviderError('rate_limited', 'acoustid', 'AcoustID ограничил частоту запросов')
                        if response.status_code in (401, 403):
                            raise ProviderError('provider_unauthorized', 'acoustid', 'AcoustID отклонил ключ приложения')
                        if response.status_code not in (200, 400):
                            raise ProviderError('provider_unavailable', 'acoustid', 'AcoustID временно недоступен')
                        raw = bytearray()
                        async for block in response.aiter_bytes():
                            raw.extend(block)
                            if len(raw) > 2*1024**2: raise ValueError()
                        data = json.loads(raw)
                        if not isinstance(data, dict): raise ValueError()
                        if data.get('status') != 'ok':
                            code = (data.get('error') or {}).get('code') if isinstance(data.get('error'), dict) else None
                            if code == 14:
                                await asyncio.to_thread(db.set_setting, settings.db_path, 'provider_breaker:acoustid',
                                    db.encode({'until': time.time()+300, 'reason': 'rate_limited'}))
                            raise ProviderError('provider_unauthorized' if code == 4 else 'rate_limited' if code == 14 else 'provider_error',
                                'acoustid', 'AcoustID отклонил запрос; проверьте ключ и лимиты')
                        if response.status_code != 200: raise ValueError()
                        rows = data.get('results')
                        if not isinstance(rows, list) or len(rows) > 100: raise ValueError()
                        if not rows: response.extensions['phonoteka_no_match'] = True
                        # Validate before durable caching; never store response errors.
                        candidates(rows, duration)
                current_authorization()
                if not probe: await asyncio.to_thread(providers.cache, cache_key, 'acoustid', rows, 'metadata')
                return rows
            except TimeoutError as exc:
                raise ProviderError("unknown_outcome" if attempt_state["unknown"] else "provider_unavailable", "acoustid", "Истекло время AcoustID; отправленная попытка не повторяется автоматически") from exc
            except (ValueError, TypeError, KeyError) as exc:
                raise ProviderError('provider_unavailable', 'acoustid', 'Ошибка формата ответа или истекло время AcoustID') from exc


def candidates(rows, duration):
    result = []
    for row in rows[:20]:
        if not isinstance(row, dict): raise ValueError()
        score = row.get('score'); identity = row.get('id')
        if (not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score)
                or not 0 <= score <= 1 or not isinstance(identity, str) or not UUID.fullmatch(identity)): raise ValueError()
        recordings = row.get('recordings', [])
        if not isinstance(recordings, list) or len(recordings) > 100: raise ValueError()
        if score < .8: continue
        for recording in recordings[:8]:
            if not isinstance(recording, dict): raise ValueError()
            mbid = recording.get('id'); title = recording.get('title'); artists = recording.get('artists', [])
            if not isinstance(mbid, str) or not UUID.fullmatch(mbid): continue
            if not isinstance(title, str) or not title or len(title) > 1000: continue
            if not isinstance(artists, list) or len(artists) > 50: raise ValueError()
            if not all(isinstance(a, dict) and isinstance(a.get('name'), str) and len(a['name']) <= 1000 for a in artists): raise ValueError()
            artist = ', '.join(a['name'] for a in artists)[:1000]
            if not artist: continue
            if any(c['recording_id'] == mbid for c in result): continue
            result.append({'id': 'acoustid:'+identity+':'+mbid, 'provider': 'acoustid', 'kind': 'metadata',
                'provider_recording_id': identity, 'recording_id': mbid, 'score': score,
                'tags': {'title': title, 'artist': artist, 'musicbrainz_trackid': mbid},
                'identification': 'audio_fingerprint', 'extractor_profile': PROFILE,
                'source_url': 'https://acoustid.org/track/'+identity, 'recording_version_verified': False,
                'match': {'state': 'fingerprint_match', 'reasons': ['recording_version_unverified']}})
            if len(result) >= 8: return result
    return result


async def identify(providers, signature, duration, authorize=lambda: None):
    rows = await lookup(providers, signature, duration, authorize)
    return candidates(rows, duration)


def probe_signature(settings):
    with tempfile.TemporaryDirectory(prefix='phonoteka-acoustid-probe-') as directory:
        path = Path(directory)/'probe.wav'
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(11025)
            output.writeframes(b'\0' * (30*11025*2))
        return fingerprint(path, {'work': Path(directory)})


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(max_length=64)
    enabled: bool
    api_key: SecretStr | None = Field(default=None, max_length=128)


def install(app, settings):
    @app.get('/api/integrations/acoustid')
    def status(request: Request):
        security.require_session(request, settings, owner_only=True)
        return public_connection(settings)

    @app.put('/api/integrations/acoustid')
    def update(request: Request, body: ConnectionInput):
        security.require_session(request, settings, True, owner_only=True)
        return save_connection(settings, body.version, body.enabled, body.api_key.get_secret_value() if body.api_key is not None else None)

    @app.post('/api/integrations/acoustid/test')
    async def test(request: Request):
        security.require_session(request, settings, True, owner_only=True)
        cfg = connection(settings)
        if not public_connection(settings, cfg)['configured']:
            raise security.error(422, 'provider_disabled', 'Сохраните ключ, включите AcoustID и разрешите передачу отпечатков на сервере')
        def authorize():
            security.require_session(request, settings, True, owner_only=True)
            if connection(settings)['version'] != cfg['version']:
                raise media.MediaError('connection_conflict', 'Подключение изменилось во время проверки')
        providers = Providers(settings)
        try:
            signature = await asyncio.to_thread(probe_signature, settings)
            await lookup(providers, signature, 30, authorize, probe=True)
            return {'state': 'ok', 'message': 'Ключ принят. AcoustID доступен; проверка выполнена на синтетическом отпечатке.'}
        except ProviderError as exc:
            raise security.error(422, exc.code, str(exc)) from exc
        finally:
            await providers.close()
