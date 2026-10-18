"""Opt-in fingerprint identification. Only a locally computed signature leaves the host."""
import asyncio
import hashlib
import io
import json
import math
import tempfile
from pathlib import Path
import time
import wave
from urllib.parse import urlsplit

import httpx

from . import db, media,provider_attempts
from .providers import ProviderError

POLICY = 'shazamio-0.8.1-sample-v1'


def sample(path, roots, duration, offset=None):
    path = media._validate(path, roots)
    if not math.isfinite(duration) or duration<=0:
        duration=float(media.probe_audio(path).get('duration',0))
    if not math.isfinite(duration) or not 3 <= duration <= 6 * 3600:
        raise media.MediaError('recognition_duration', 'Для отпечатка нужна запись от 3 секунд до 6 часов')
    offset = min(45., max(0., duration * .25)) if offset is None else offset
    length = min(12., duration - offset)
    # Explicit local demuxer/protocol validation precedes ffmpeg. No playlist or URL input.
    args = ['-nostdin', '-threads', '1', '-ss', str(offset), '-t', str(length), '-map', '0:a:0',
            '-vn', '-sn', '-dn', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1']
    proc = media._process(path, 'ffmpeg', args, timeout=45)
    # Some MP3 headers advertise frames beyond the decodable tail. Preserve the
    # actual offset in evidence and try the beginning once instead of losing it.
    if len(proc.stdout)<32000 and offset>0:return sample(path,roots,duration,0)
    if proc.returncode or not 32000 <= len(proc.stdout) <= 384000 or len(proc.stdout) % 2:
        raise media.MediaError('decode_failed', 'Некорректный декодированный фрагмент')
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(proc.stdout)
    return output.getvalue(), offset


class SignatureTransport:
    """Replace the library's 20 retries with one bounded HTTPS signature request."""
    def __init__(self, providers, authorize=lambda: None,revision=None,input_kind='sample_sha256'):
        self.providers = providers
        self.authorize = authorize
        self.revision=revision;self.input_kind=input_kind

    async def request(self, method, url, *args, **kwargs):
        with self.providers.metrics.provider('shazam','fingerprint'):
            return await self._request(method,url,*args,**kwargs)

    async def _request(self, method, url, *args, **kwargs):
        parsed = urlsplit(url)
        payload = kwargs.get('json')
        if (method != 'POST' or parsed.scheme != 'https' or parsed.hostname != 'amp.shazam.com'
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or not parsed.path.startswith('/discovery/v5/') or not isinstance(payload, dict)
                or set(payload) != {'timezone', 'signature', 'timestamp', 'context', 'geolocation'}
                or payload['context'] or payload['geolocation']):
            raise ProviderError('unsafe_provider_request', 'shazam', 'Недопустимый запрос распознавания')
        signature = payload.get('signature', {})
        if (set(signature) != {'uri', 'samplems'} or not isinstance(signature.get('uri'), str)
                or not signature['uri'].startswith('data:audio/vnd.shazam.sig;base64,')
                or len(signature['uri']) > 100000 or not 0 < signature.get('samplems', 0) <= 13000):
            raise ProviderError('unsafe_provider_request', 'shazam', 'Недопустимый аудиоотпечаток')
        self.authorize()
        from . import provider_policy
        try:await asyncio.to_thread(provider_policy.check,self.providers.settings,'shazam','metadata','fetch')
        except media.MediaError as exc:raise ProviderError(exc.code,'shazam',exc.message) from exc
        try:
            async with self.providers.request('shazam','fingerprint','POST',url,headers=kwargs.get('headers'),json=payload,
                    revision=self.revision,input_kind=self.input_kind,authorize=self.authorize) as response:
                if response.status_code == 429:
                    value = response.headers.get('retry-after', '300')
                    delay = min(86400, max(60, int(value))) if value.isdigit() else 300
                    await asyncio.to_thread(db.set_setting, self.providers.settings.db_path, 'provider_breaker:shazam',
                                            db.encode({'until': time.time() + delay, 'reason': 'rate_limited'}))
                    interval=float(await asyncio.to_thread(db.setting,self.providers.settings.db_path,'provider_interval:shazam','10'))
                    self.providers.intervals['shazam']=min(120,interval*2)
                    await asyncio.to_thread(db.set_setting,self.providers.settings.db_path,'provider_interval:shazam',str(self.providers.intervals['shazam']))
                    raise ProviderError('rate_limited', 'shazam', 'Shazam ограничил частоту запросов')
                if response.status_code in (401, 403):
                    raise ProviderError('provider_unauthorized', 'shazam', 'Shazam отклонил доступ')
                if response.status_code != 200:
                    raise ProviderError('provider_unavailable', 'shazam', 'Shazam временно недоступен: HTTP '+str(response.status_code))
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > 2 * 1024**2:
                        raise ProviderError('response_too_large', 'shazam', 'Ответ Shazam превышает лимит')
                result = json.loads(raw)
                if not isinstance(result, dict): raise ValueError('Expected object')
                track=result.get('track') or {}
                if not isinstance(track,dict) or not all(track.get(field) for field in ('key','title','subtitle')):
                    response.extensions['phonoteka_no_match']=True
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError('provider_unavailable', 'shazam', 'Ошибка сети или ответа Shazam: '+type(exc).__name__) from exc


async def recognize(providers, audio, offset, authorize=lambda: None,*,input_revision=None):
    from shazamio import Shazam
    from . import provider_policy,media
    try:usage=await asyncio.to_thread(provider_policy.check,providers.settings,'shazam','metadata','fetch')
    except media.MediaError as exc:raise ProviderError(exc.code,'shazam',exc.message) from exc
    key = hashlib.sha256((POLICY+usage['policy_version']).encode() + audio).hexdigest()
    # Single flight also avoids concurrent identical samples consuming quota twice.
    lock = providers.locks.setdefault('shazam-flight', asyncio.Lock())
    async with lock:
        cached = await asyncio.to_thread(db.fetchone, providers.settings.db_path,
                                        'SELECT payload FROM provider_cache WHERE key=? AND expires>?', (key, time.time())) if usage['allow_cache'] else None
        if cached:
            data = json.loads(cached['payload'])
        else:
            # Cached evidence spends no HTTP allowance. A miss checks capacity
            # before ShazamIO can return a local empty/no-query result, while
            # reserve still performs atomic admission before every real send.
            try:
                await asyncio.to_thread(provider_attempts.require_recognition_capacity,providers.settings,
                                        provider_attempts.RECOGNITION.get()[0])
            except provider_attempts.UsageError as exc:
                raise ProviderError(exc.code,'shazam','Бюджет распознавания этой аудиозаписи исчерпан или не подтверждена аудиоревизия') from exc
            breaker = json.loads(await asyncio.to_thread(db.setting, providers.settings.db_path, 'provider_breaker:shazam', '{}'))
            if breaker.get('until', 0) > time.time():
                raise ProviderError('provider_cooldown', 'shazam', 'Shazam временно приостановлен')
            with provider_attempts.request_scope() as attempt_state:
                try:
                    revision=input_revision or hashlib.sha256(audio).hexdigest()
                    shazam = Shazam(http_client=SignatureTransport(providers,authorize,revision,'file_sha256' if input_revision else 'sample_sha256'), segment_duration_seconds=12)
                    # WAV filename avoids the library probing PCM as MPEG.
                    with tempfile.TemporaryDirectory(prefix='phonoteka-fingerprint-') as tmp:
                        wav=Path(tmp)/'sample.wav';wav.write_bytes(audio)
                        data = await asyncio.wait_for(shazam.recognize(str(wav)), timeout=50)
                    await asyncio.to_thread(providers.cache, key, 'shazam', data)
                except (ProviderError, asyncio.TimeoutError) as exc:
                    code=getattr(exc,'code','unknown_outcome' if attempt_state['unknown'] else 'provider_unavailable')
                    if code=='provider_unavailable':
                        await asyncio.to_thread(providers.unavailable,'shazam')
                    elif code=='provider_unauthorized':
                        await asyncio.to_thread(db.set_setting, providers.settings.db_path, 'provider_breaker:shazam',
                                                db.encode({'until': time.time() + 1200, 'reason': code}))
                    if isinstance(exc, ProviderError):raise
                    raise ProviderError(code,'shazam','Превышено время распознавания; отправленная попытка не повторяется автоматически') from exc
        track = data.get('track') or {}
        if not track.get('key') or not track.get('title') or not track.get('subtitle'):
            return None
        tags = {'title': str(track['title'])[:1000], 'artist': str(track['subtitle'])[:1000]}
        for section in track.get('sections', []):
            if section.get('type') == 'SONG':
                for entry in section.get('metadata', []):
                    if entry.get('title') == 'Album' and entry.get('text'):
                        tags['album'] = str(entry['text'])[:1000]
        source = str(track.get('url', ''))
        if urlsplit(source).scheme != 'https' or urlsplit(source).hostname not in {'www.shazam.com', 'shazam.com'}: source = ''
        with wave.open(io.BytesIO(audio)) as wav: seconds=wav.getnframes()/wav.getframerate()
        return {'id': 'shazam:' + str(track['key'])[:100], 'provider': 'shazam', 'kind': 'metadata', 'tags': tags,
                'identification': 'audio_fingerprint', 'source_url': source, 'sample_offset': offset,
                'sample_seconds': seconds, 'recording_version_verified': False,
                'match': {'state': 'fingerprint_match', 'reasons': ['recording_version_unverified']}}
