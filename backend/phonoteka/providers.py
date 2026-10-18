"""Bounded asynchronous public metadata providers. Results are proposals, not truth."""
import asyncio
import base64
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlsplit

import httpx
from . import db, media

POLICY_VERSION = 'public-v1'
USER_AGENT = 'Phonoteka/0.2 (https://github.com/nd4y/phonoteka; personal music catalog)'
HOSTS = {'beta.musicbrainz.org','musicbrainz.org','lrclib.net','coverartarchive.org','archive.org','api.acoustid.org'}
INTERVAL = {'musicbrainz':1.1,'lrclib':.5,'coverartarchive':1.1,'acoustid':.4,'shazam':2.0}


def normalized(value):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', str(value)).casefold()))


def scalar(tags, key):
    value = tags.get(key, '')
    return str(value[0] if isinstance(value, list) and value else value or '')


def query_tags(meta, relative):
    tags = {k:scalar(meta.get('tags',{}), k) for k in ('title','artist','album')}
    provenance = 'embedded'
    if not tags['title']:
        title = re.sub(r'^\s*\d+[. _-]+','',Path(relative).stem).strip()
        parts = re.split(r'\s+-\s+',title,maxsplit=1)
        if len(parts)==2 and not tags['artist']:
            tags['artist'],title=parts
        tags['title']=title
        provenance='filename_hint'
    return tags,provenance


def assess(query, title, artist, expected_duration, duration):
    title_match=normalized(query['title'])==normalized(title)
    artist_match=normalized(query['artist'])==normalized(artist)
    delta=abs(float(duration)-float(expected_duration)) if duration and expected_duration else None
    reasons=[]
    if not title_match: reasons.append('title_differs')
    if not artist_match: reasons.append('artist_differs')
    if delta is None: reasons.append('duration_unknown')
    elif delta>2: reasons.append('duration_differs')
    return {'state':'matching_metadata' if not reasons else 'needs_review','reasons':reasons,'duration_delta':delta,'title_similarity':round(SequenceMatcher(None,normalized(query['title']),normalized(title)).ratio(),3)}


class ProviderError(Exception):
    def __init__(self, code, provider, message):
        super().__init__(message);self.code=code;self.provider=provider


class Providers:
    def __init__(self, settings, transport=None):
        self.settings=settings
        self.client=httpx.AsyncClient(timeout=httpx.Timeout(20,connect=8),limits=httpx.Limits(max_connections=4,max_keepalive_connections=4),headers={'User-Agent':USER_AGENT},transport=transport,follow_redirects=False)
        self.locks={};self.next={};self.semaphore=asyncio.Semaphore(4)

    async def close(self): await self.client.aclose()

    def unavailable(self, provider):
        previous=json.loads(db.setting(self.settings.db_path,'provider_breaker:'+provider,'{}'))
        failures=min(6,previous.get('failures',0)+1)
        db.set_setting(self.settings.db_path,'provider_breaker:'+provider,db.encode({'until':time.time()+min(1200,60*2**(failures-1)),'reason':'provider_unavailable','failures':failures}))

    def reserve(self, provider):
        now=time.time()
        with db.transaction(self.settings.db_path) as conn:
            conn.execute('DELETE FROM provider_calls WHERE at<?',(now-86400,))
            used=conn.execute('SELECT COUNT(*) FROM provider_calls WHERE provider=?',(provider,)).fetchone()[0]
            if used>=self.settings.provider_daily_limit:
                raise ProviderError('budget_exhausted',provider,'Достигнут суточный лимит запросов')
            conn.execute('INSERT INTO provider_calls(provider,at) VALUES(?,?)',(provider,now))

    async def throttle(self, provider):
        lock=self.locks.setdefault(provider,asyncio.Lock())
        async with lock:
            delay=self.next.get(provider,0)-time.monotonic()
            if delay>0: await asyncio.sleep(delay)
            self.next[provider]=time.monotonic()+INTERVAL.get(provider,1)
        await asyncio.to_thread(self.reserve,provider)

    async def get(self, provider, url, params=None, *, binary=False):
        key=hashlib.sha256(db.encode([POLICY_VERSION,url,params,binary]).encode()).hexdigest()
        cached=await asyncio.to_thread(db.fetchone,self.settings.db_path,'SELECT payload FROM provider_cache WHERE key=? AND expires>?',(key,time.time()))
        if cached: return json.loads(cached['payload'])
        breaker=json.loads(await asyncio.to_thread(db.setting,self.settings.db_path,'provider_breaker:'+provider,'{}'))
        if breaker.get('until',0)>time.time(): raise ProviderError('provider_cooldown',provider,'Источник временно отключен после повторных ошибок')
        target=url
        for attempt in range(3):
            try:
                for redirect in range(5):
                    host=urlsplit(target).hostname or ''
                    if urlsplit(target).scheme!='https' or (host not in HOSTS and not host.endswith('.archive.org')):
                        raise ProviderError('unsafe_provider_url',provider,'Источник вернул запрещенный адрес')
                    await self.throttle(provider)
                    async with self.semaphore, self.client.stream('GET',target,params=params if target==url else None) as response:
                        if response.status_code in (301,302,303,307,308):
                            target=str(response.url.join(response.headers.get('location','')))
                            continue
                        if response.status_code==404: data=None;break
                        if response.status_code in (401,403): raise ProviderError('provider_unauthorized',provider,'Источник отклонил доступ')
                        if response.status_code==429:
                            retry=response.headers.get('retry-after','60')
                            seconds=min(86400,max(60,int(retry))) if retry.isdigit() else 300
                            await asyncio.to_thread(db.set_setting,self.settings.db_path,'provider_breaker:'+provider,db.encode({'until':time.time()+seconds,'reason':'rate_limited'}))
                            raise ProviderError('rate_limited',provider,'Источник ограничил частоту запросов')
                        if response.status_code>=500: raise ProviderError('provider_unavailable',provider,'Источник временно недоступен: HTTP '+str(response.status_code))
                        if response.status_code!=200: raise ProviderError('provider_error',provider,'Неожиданный ответ источника')
                        content=bytearray();limit=5*1024**2 if binary else 2*1024**2
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content)>limit: raise ProviderError('response_too_large',provider,'Ответ источника превышает лимит')
                        data=base64.b64encode(content).decode() if binary else json.loads(content)
                        break
                else: raise ProviderError('redirect_limit',provider,'Слишком много перенаправлений')
                await asyncio.to_thread(db.set_setting,self.settings.db_path,'provider_breaker:'+provider,'{}')
                await asyncio.to_thread(self.cache,key,provider,data)
                return data
            except (httpx.HTTPError,ValueError) as exc:
                if attempt==2: raise ProviderError('provider_unavailable',provider,'Ошибка сети или формата ответа: '+type(exc).__name__) from exc
            except ProviderError as exc:
                if exc.code!='provider_unavailable' or attempt==2: raise
            await asyncio.sleep(2**(attempt+1))
        raise ProviderError('provider_unavailable',provider,'Источник недоступен')

    def cache(self,key,provider,data):
        with db.transaction(self.settings.db_path) as conn:
            conn.execute('DELETE FROM provider_cache WHERE expires<?',(time.time(),))
            # Cap total entries, including large artwork, without unbounded growth.
            if conn.execute('SELECT COUNT(*) FROM provider_cache').fetchone()[0]>=4000:
                conn.execute('DELETE FROM provider_cache WHERE key IN (SELECT key FROM provider_cache ORDER BY expires LIMIT 100)')
            payload=db.encode(data)
            # A byte budget also bounds artwork; SQLite pages are reused after eviction.
            while (conn.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM provider_cache').fetchone()[0]+len(payload.encode()))>256*1024**2:
                conn.execute('DELETE FROM provider_cache WHERE key IN (SELECT key FROM provider_cache ORDER BY expires LIMIT 100)')
            conn.execute('INSERT OR REPLACE INTO provider_cache VALUES(?,?,?,?)',(key,provider,payload,time.time()+(86400 if data is None else 7*86400)))

    async def musicbrainz(self, query, duration):
        def escape(value): return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])',r'\\\1',value[:180])
        q='recording:"'+escape(query['title'])+'"'
        if query['artist']: q+=' AND artist:"'+escape(query['artist'])+'"'
        data=await self.get('musicbrainz','https://'+self.settings.musicbrainz_host+'/ws/2/recording',{'query':q,'fmt':'json','limit':8})
        candidates=[]
        for row in (data or {}).get('recordings',[])[:8]:
            artist=''.join(x.get('name',x.get('artist',{}).get('name',''))+x.get('joinphrase','') for x in row.get('artist-credit',[]))
            match=assess(query,row.get('title',''),artist,duration,(row.get('length') or 0)/1000)
            releases=[{'id':v['id'],'title':v.get('title',''),'date':v.get('date',''),'country':v.get('country',''),'status':v.get('status','')} for v in row.get('releases',[]) if v.get('id')][:15]
            candidates.append({'id':'mb:'+row['id'],'provider':'musicbrainz','kind':'metadata','recording_id':row['id'],'tags':{'title':row.get('title',''),'artist':artist,'musicbrainz_trackid':row['id']},'duration':(row.get('length') or 0)/1000,'match':match,'releases':releases,'source_url':'https://musicbrainz.org/recording/'+row['id'],'identification':'text_search'})
        return candidates

    async def lyrics(self, query, duration):
        data=None
        if query.get('album') and query.get('artist') and duration:
            exact=await self.get('lrclib','https://lrclib.net/api/get',{'track_name':query['title'],'artist_name':query['artist'],'album_name':query['album'],'duration':round(duration)})
            if isinstance(exact,dict) and exact.get('id') is not None:data=[exact]
        if data is None:
            data=await self.get('lrclib','https://lrclib.net/api/search',{'track_name':query['title'],'artist_name':query['artist']})
        candidates=[]
        for row in (data or [])[:20]:
            if not isinstance(row,dict): continue
            match=assess(query,row.get('trackName',''),row.get('artistName',''),duration,row.get('duration'))
            lrc=row.get('syncedLyrics') or ''
            if lrc:
                try:
                    doc=media.parse_lrc(lrc)
                    if not doc['complete'] or any(x['start_ms']>duration*1000+1000 for x in doc['lines']):
                        match['state']='needs_review';match['reasons'].append('lyrics_out_of_range')
                except media.MediaError:
                    match['state']='needs_review';match['reasons'].append('invalid_lyrics')
            candidates.append({'id':'lrclib:'+str(row['id']),'provider':'lrclib','kind':'lyrics','title':row.get('trackName'),'artist':row.get('artistName'),'album':row.get('albumName'),'duration':row.get('duration'),'lrc':lrc,'plain_lyrics':row.get('plainLyrics') or '', 'instrumental_claim':bool(row.get('instrumental')),'match':match,'source_url':'https://lrclib.net/api/get/'+str(row['id']),'identification':'text_search','timing_verified':False})
        return sorted(candidates,key=lambda x:(len(x['match']['reasons']),x['match']['duration_delta'] or 0))[:8]

    async def cover(self, release_id):
        if not re.fullmatch('[0-9a-fA-F-]{36}',release_id): raise ProviderError('invalid_release','coverartarchive','Неверный идентификатор релиза')
        url='https://coverartarchive.org/release/'+release_id
        doc=await self.get('coverartarchive',url)
        fronts=[x for x in (doc or {}).get('images',[]) if x.get('front') and x.get('approved')]
        if not fronts: return None
        image=fronts[0].get('thumbnails',{}).get('500') or fronts[0].get('image')
        if not image: return None
        raw=await self.get('coverartarchive',image.replace('http://','https://',1),binary=True)
        if not raw: return None
        mime,_,_=media._cover(base64.b64decode(raw))
        return {'id':'caa:'+release_id,'provider':'coverartarchive','kind':'cover','cover':'data:'+mime+';base64,'+raw,'release_id':release_id,'source_url':url,'match':{'state':'needs_review','reasons':['confirm_release']}}

    async def analyze(self, meta, relative, identified=None):
        query,provenance=query_tags(meta,relative)
        if identified:
            query={k:scalar(identified['tags'],k) for k in ('title','artist','album')}
            provenance='audio_fingerprint'
        result={'input_sha256':meta['sha256'],'policy':POLICY_VERSION,'query':query,'query_source':provenance,'candidates':[],'sources':[],'state':'needs_review','automatic_apply':False}
        if meta.get('input_error'):
            result.update(state='invalid_audio',sources=[{'provider':'local','state':'invalid_audio'}]);return result
        if not query['title']:
            result['sources']=[{'provider':'musicbrainz','state':'insufficient_metadata'},{'provider':'lrclib','state':'insufficient_metadata'},{'provider':'acoustid','state':'key_required'}]
            return result
        for provider,call in [('musicbrainz',self.musicbrainz),('lrclib',self.lyrics)]:
            try:
                candidates=await call(query,meta.get('duration',0))
                result['candidates'].extend(candidates)
                result['sources'].append({'provider':provider,'state':'candidates' if candidates else 'no_match','count':len(candidates)})
            except ProviderError as exc:
                if exc.code=='provider_unavailable':
                    await asyncio.to_thread(self.unavailable,provider)
                result['sources'].append({'provider':provider,'state':exc.code,'message':str(exc)})
        matching=[c for c in result['candidates'] if c['kind']=='metadata' and c['match']['state']=='matching_metadata']
        releases={v['id'] for c in matching for v in c.get('releases',[]) if query['album'] and normalized(v['title'])==normalized(query['album'])}
        if not meta.get('has_art') and len(releases)==1:
            try:
                art=await self.cover(next(iter(releases)))
                if art: result['candidates'].append(art)
                result['sources'].append({'provider':'coverartarchive','state':'candidate' if art else 'no_match'})
            except (ProviderError, media.MediaError) as exc: result['sources'].append({'provider':'coverartarchive','state':exc.code})
        result['state']='candidates' if result['candidates'] else 'no_match' if all(x['state']=='no_match' for x in result['sources']) else 'incomplete' 
        result['sources'].append({'provider':'acoustid','state':'key_required'})
        return result
