"""YouTube provenance is separate from identifying a musical recording."""
import asyncio
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit

import mutagen
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from . import db, media, security
from .providers import ProviderError, query_tags, scalar

ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
URL = re.compile(r'https?://[^\s<>"\x00-\x1f]+', re.I)
HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be', 'www.youtube-nocookie.com'}
POLICY = 'youtube-provenance-v1'


def source_tag(key):
    key=str(key).lower()
    if key.startswith(('txxx:', 'wxxx:', '----:com.apple.itunes:')):
        return key.rsplit(':',1)[-1] in {'purl','url','source','source_url','webpage_url','original_url','comment'}
    return key.startswith(('comm:', 'woas', 'woaf')) or key in {'purl','comment','source','url','source_url','webpage_url','original_url','©cmt'}


def video_id(value):
    if not isinstance(value, str) or len(value) > 2048: return None
    try:
        u = urlsplit(value.strip())
        if u.scheme not in {'https', 'http'} or u.hostname not in HOSTS or u.username or u.password or u.port not in {None, 80, 443}: return None
        parts = u.path.strip('/').split('/')
        identity = parts[0] if u.hostname.endswith('youtu.be') and len(parts) == 1 else parse_qs(u.query).get('v', [''])[0] if u.path == '/watch' else parts[1] if len(parts) == 2 and parts[0] in {'shorts', 'live', 'embed', 'v'} else ''
        return identity if ID.fullmatch(identity) else None
    except ValueError: return None


def canonical(identity):
    if not ID.fullmatch(identity): raise ValueError('Invalid YouTube ID')
    return 'https://www.youtube.com/watch?v=' + identity


def _strings(value):
    if isinstance(value, (str, bytes)):
        yield value[:32768].decode('utf-8', errors='replace') if isinstance(value, bytes) else value[:32768]
    elif isinstance(value, (list, tuple)):
        for v in value[:20]: yield from _strings(v)
    else:
        for attr in ('url', 'text'):
            if hasattr(value, attr): yield from _strings(getattr(value, attr))


def discover(path, settings, metadata):
    """Read bounded local hints only. Never execute or fetch sidecar URLs."""
    path = media._validate(path, settings.roots)
    inherited = metadata.get('derived_from') or {}
    if inherited.get('source'):
        relative = Path(inherited['source'])
        if relative.is_absolute() or '..' in relative.parts: raise media.MediaError('unsafe_path', 'Неверный путь источника')
        path = media._validate(settings.work_root / relative, settings.roots)
    found = {}; warnings = []
    def add(identity, kind):
        if not isinstance(identity, str) or not ID.fullmatch(identity): return
        entry = found.setdefault(identity, {'video_id': identity, 'url': canonical(identity), 'evidence': [], 'availability': 'unchecked'})
        if kind not in entry['evidence']: entry['evidence'].append(kind)
        if inherited: entry['inherited'] = True
    def urls(value, kind):
        for text in _strings(value):
            for u in URL.findall(text)[:20]: add(video_id(u.rstrip(').,;\'')), kind)
    # IDs are accepted only in known downloader suffix shapes, never arbitrary text.
    stem = path.stem if path.suffix.lower() in {'.mp3','.flac','.m4a','.mp4','.webm','.opus','.ogg','.wav','.aac','.aiff','.wma'} else path.name
    match = re.search(r'\[([A-Za-z0-9_-]{11})\](?:\.f\d+)?$', stem)
    if not match: match = re.search(r'-([A-Za-z0-9_-]{11})(?:\.f\d+)?$', stem)
    if match: add(match[1], 'filename_id')
    try:
        with media._file(path) as handle: audio = mutagen.File(handle)
        if audio and audio.tags:
            for key, value in list(audio.tags.items())[:256]:
                if source_tag(key):
                    urls(value, 'embedded_url')
        elif metadata.get('format') in {'webm', 'mka'} or path.suffix.lower() in {'.webm','.mka'}:
            raw = media._process(path, 'ffprobe', ['-show_entries','format_tags:stream_tags','-of','json']).stdout
            if len(raw) <= 2*1024**2:
                probe = json.loads(raw)
                for tags in [probe.get('format', {}).get('tags', {})] + [s.get('tags', {}) for s in probe.get('streams', [])]:
                    for key, value in tags.items():
                        if key.lower() in {'purl','comment','source','url','webpage_url'}: urls(value, 'embedded_url')
    except (mutagen.MutagenError, ValueError, OSError, media.MediaError): warnings.append('embedded_tags_unreadable')
    candidates = [path.with_suffix('.info.json'), Path(str(path)+'.info.json')]
    format_stem = re.sub(r'\.f\d+$', '', path.stem)
    if format_stem != path.stem: candidates.append(path.with_name(format_stem+'.info.json'))
    for sidecar in dict.fromkeys(candidates):
        if not sidecar.exists() and not sidecar.is_symlink(): continue
        try:
            sidecar = media._validate(sidecar, settings.roots)
            if sidecar.stat().st_size > 2*1024**2: warnings.append('sidecar_too_large'); continue
            with media._file(sidecar) as handle: raw = handle.read(2*1024**2+1)
            if len(raw) > 2*1024**2: warnings.append('sidecar_too_large'); continue
            info = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(info, dict) or info.get('_type') in {'playlist','multi_video'}: continue
            for key in ('webpage_url', 'original_url'): urls(info.get(key, ''), 'sidecar_url')
            if str(info.get('extractor_key') or info.get('extractor','')).lower() == 'youtube': add(info.get('id'), 'sidecar_id')
        except (ValueError, RecursionError, OSError, media.MediaError): warnings.append('sidecar_unreadable')
    if inherited and found:
        try:
            if not inherited.get('source_sha256'): raise media.MediaError('source_revision_unknown','Неизвестна версия источника')
            media._expect(path,inherited['source_sha256'])
        except media.MediaError:
            found={};warnings.append('source_revision_changed')
    return {'policy': POLICY, 'candidates': list(found.values())[:12], 'warnings': sorted(set(warnings)), 'scanned_at': db.utcnow()}


def merge(local, old=None):
    old = old or {}; previous = {x['video_id']: x for x in old.get('candidates', [])}
    result = {**local, 'candidates': [{**previous.get(x['video_id'], {}), **x, **({k:previous[x['video_id']][k] for k in ('title','channel','availability','checked_at') if k in previous[x['video_id']]} if x['video_id'] in previous else {})} for x in local['candidates']]}
    local_ids = {c['video_id'] for c in result['candidates']}
    result['candidates'] += [c for c in old.get('candidates', []) if c['video_id'] not in local_ids and any(e in {'owner','search'} for e in c.get('evidence', []))][:5]
    selected = old.get('linked_id') if old.get('state') == 'owner_confirmed' else None
    recorded = [c['video_id'] for c in local['candidates'] if set(c['evidence']) & {'embedded_url','sidecar_url','sidecar_id'}]
    if old.get('state') == 'unlinked': result.update(linked_id=None,state='unlinked')
    elif selected and any(c['video_id'] == selected for c in result['candidates']):
        result.update(linked_id=selected, state='owner_confirmed')
    elif len(local_ids) > 1: result.update(linked_id=None, state='conflict')
    elif len(recorded) == 1: result.update(linked_id=recorded[0], state='recorded')
    else: result.update(linked_id=None, state='candidates' if result['candidates'] else 'unknown')
    return result


def search_query(meta, relative, analysis):
    identified = next((c for c in (analysis or {}).get('candidates', []) if c.get('provider') == 'shazam' and c.get('tags')), None)
    if identified:
        tags = identified['tags']; origin = 'audio_fingerprint'
    else:
        tags, origin = query_tags(meta, relative)
    title = scalar(tags, 'title'); artist = scalar(tags, 'artist')
    title = re.sub(r'\s*\[[A-Za-z0-9_-]{11}\]\s*$', '', title)
    query = ' '.join((artist+' '+title).split())[:180]
    return query if len(query) >= 3 else '', origin


async def flat_search(query):
    """Only public search metadata, in a bounded child process; no media/cookies."""
    args = [sys.executable, '-I', '-m', 'yt_dlp', '--ignore-config', '--no-plugin-dirs', '--no-cache-dir', '--no-cookies', '--no-warnings', '--skip-download', '--flat-playlist', '--playlist-end','3', '--dump-single-json', '--socket-timeout','8', '--retries','0', '--extractor-retries','0', '--use-extractors','youtube:search', '--', 'ytsearch3:'+query]
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2*1024**2)
    try:
        async def read():
            data = bytearray()
            while True:
                block = await process.stdout.read(65536)
                if not block: break
                data.extend(block)
                if len(data) > 2*1024**2: raise ValueError('Response too large')
            await process.wait()
            if process.returncode: raise ValueError('Search failed')
            return json.loads(data)
        result = await asyncio.wait_for(read(), timeout=35)
        return result.get('entries', [])[:3]
    except (ValueError, asyncio.TimeoutError, OSError):
        raise ProviderError('provider_unavailable', 'youtube', 'Поиск YouTube временно недоступен') from None
    finally:
        if process.returncode is None: process.kill(); await process.wait()


async def enrich(providers, data, meta, relative, analysis, search=False, authorize=lambda:None):
    if not providers.settings.youtube_enabled: raise ProviderError('provider_disabled', 'youtube', 'YouTube отключен')
    if search:
        data['candidates'] = [c for c in data['candidates'] if c.get('evidence') != ['search']]
    for candidate in data['candidates'][:12]:
        if candidate.get('availability') == 'available' and candidate.get('checked_epoch', 0) > time.time()-7*86400: continue
        authorize()
        info = await providers.get('youtube', 'https://www.youtube.com/oembed', {'url':canonical(candidate['video_id']), 'format':'json'})
        candidate.update(availability='available' if info else 'unavailable', checked_at=db.utcnow(), checked_epoch=time.time())
        if isinstance(info, dict): candidate.update(title=str(info.get('title',''))[:500], channel=str(info.get('author_name',''))[:200])
    # Finding the same song never establishes that this was the downloaded video.
    if search and not data['candidates']:
        query, origin = search_query(meta, relative, analysis);data['query_source'] = origin
        if not query: data['search_state'] = 'insufficient_query'; return data
        key = hashlib.sha256(json.dumps([POLICY, query], ensure_ascii=False).encode()).hexdigest()
        cache = await asyncio.to_thread(db.fetchone, providers.settings.db_path, 'SELECT payload FROM provider_cache WHERE key=? AND expires>?', (key,time.time()))
        if cache: entries = json.loads(cache['payload'])
        else:
            breaker = json.loads(await asyncio.to_thread(db.setting,providers.settings.db_path,'provider_breaker:youtube','{}'))
            if breaker.get('until',0) > time.time(): raise ProviderError('provider_cooldown','youtube','YouTube временно недоступен')
            with providers.metrics.provider('youtube','search'):
                authorize();await providers.throttle('youtube');authorize();entries = await flat_search(query)
            await asyncio.to_thread(providers.cache,key,'youtube',entries)
        for entry in entries:
            identity = entry.get('id')
            if not isinstance(identity, str) or not ID.fullmatch(identity): continue
            duration = entry.get('duration'); duration = duration if isinstance(duration,(int,float)) and math.isfinite(duration) and duration >= 0 else None
            delta = abs(duration-meta['duration']) if duration is not None and meta.get('duration') else None
            data['candidates'].append({'video_id':identity,'url':canonical(identity),'title':str(entry.get('title',''))[:500],'channel':str(entry.get('channel') or entry.get('uploader') or '')[:200], 'duration':duration,'duration_delta':delta,'evidence':['search'],'availability':'search_result'})
        data['search_state'] = 'candidates' if data['candidates'] else 'no_match'
        data['state'] = 'candidates' if data['candidates'] else 'unknown'
    return data


class LinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str | None = Field(default=None, max_length=2048)


def install(app, settings):
    @app.put('/api/tracks/{track_id}/youtube')
    def link(request:Request, track_id:str, body:LinkInput):
        security.require_session(request,settings,True,owner_only=True)
        identity = video_id(body.url) if body.url else None
        if body.url and not identity: raise security.error(422,'invalid_youtube_url','Нужна ссылка на отдельное видео YouTube')
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=? AND is_current=1',(track_id,)).fetchone()
            if not row: raise security.error(404,'track_not_found','Трек не найден')
            if request.headers.get('if-match','').strip('"') != row['sha256']: raise security.error(409,'revision_conflict','Версия файла изменилась')
            meta = json.loads(row['metadata']); data = meta.get('youtube', {'policy':POLICY,'candidates':[]})
            if identity:
                candidate = next((c for c in data['candidates'] if c['video_id'] == identity), None)
                if candidate is None:
                    candidate = {'video_id':identity,'url':canonical(identity),'evidence':[],'availability':'unchecked'};data['candidates']=data['candidates'][:12]+[candidate]
                if 'owner' not in candidate['evidence']: candidate['evidence'].append('owner')
                data.update(linked_id=identity,state='owner_confirmed')
            else:
                data['candidates']=[{**c,'evidence':[e for e in c['evidence'] if e!='owner']} for c in data['candidates']]
                data['candidates']=[c for c in data['candidates'] if c['evidence']]
                data.update(linked_id=None,state='unlinked')
            meta['youtube']=data
            conn.execute('UPDATE tracks SET metadata=?,updated_at=? WHERE id=?',(db.encode(meta),db.utcnow(),track_id))
            db.event(conn,'youtube_link_changed',{'track_id':track_id,'video_id':identity,'source':'owner'})
        return data
