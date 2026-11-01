import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
import threading
import httpx
import pytest
from phonoteka import db,providers
from phonoteka.config import Settings
from phonoteka.providers import Providers, ProviderError, assess, query_tags


def settings(tmp_path):
    cfg=Settings(state_dir=tmp_path,work_root=tmp_path/'work',source_root=tmp_path/'source',publication_root=tmp_path/'ready',storage_guard_dir=tmp_path/'guard',external_metadata=True)
    db.initialize(cfg.db_path)
    return cfg


def test_variant_and_duration_conflicts():
    q={'title':'Song','artist':'Singer'}
    assert assess(q,'Song','Singer',100,101)['state']=='matching_metadata'
    assert 'title_differs' in assess(q,'Song (Live)','Singer',100,100)['reasons']
    assert 'duration_differs' in assess(q,'Song','Singer',100,130)['reasons']
    assert 'artist_differs' in assess(q,'Song','Cover band',100,100)['reasons']


def test_async_search_cache_provenance_and_budget(tmp_path):
    calls=[]
    def response(request):
        calls.append(str(request.url))
        if request.url.host=='musicbrainz.org':
            return httpx.Response(200,json={'recordings':[{'id':'a','title':'Song','length':100000,'artist-credit':[{'name':'Singer'}],'releases':[]}]})
        return httpx.Response(200,json=[{'id':5,'trackName':'Song','artistName':'Singer','duration':100,'syncedLyrics':'[00:01.00]Line','plainLyrics':'Line'}])
    async def run():
        config=settings(tmp_path)
        with db.transaction(config.db_path) as conn:
            conn.execute('INSERT INTO settings VALUES(?,?)',('provider_usage:lrclib:lyrics',db.encode({'allow_cache':True,'cache_ttl':86400,'basis':'Explicit cache grant for this transport fixture'})))
        p=Providers(config,httpx.MockTransport(response))
        meta={'sha256':'a'*64,'tags':{'title':['Song'],'artist':['Singer']},'duration':100,'has_art':False}
        first=await p.analyze(meta,'song.flac');second=await p.analyze(meta,'song.flac')
        assert first==second and len(calls)==2
        assert first['automatic_apply'] is False
        assert first['candidates'][0]['identification']=='text_search'
        assert first['candidates'][1]['timing_verified'] is False
        await p.close()
    asyncio.run(run())


def test_redirect_ssrf_and_durable_budget(tmp_path):
    async def run():
        p=Providers(replace(settings(tmp_path),provider_daily_limit=1),httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':'http://127.0.0.1/secrets'})))
        with pytest.raises(ProviderError) as exc: await p.get('lrclib','https://lrclib.net/api/search')
        assert exc.value.code=='unsafe_provider_url'
        with pytest.raises(ProviderError) as exc: p.reserve('lrclib')
        assert exc.value.code=='budget_exhausted'
        await p.close()
    asyncio.run(run())


def test_query_inference_never_claims_embedded_evidence():
    query,source=query_tags({'tags':{}},'01. Singer - Song.mp3')
    assert query['artist']=='Singer' and query['title']=='Song'
    assert source=='filename_hint'


def test_exact_lyrics_before_broad_search_and_404_fallback(tmp_path):
    calls=[]
    def response(request):
        calls.append(request.url.path)
        if request.url.path=='/api/get':
            if request.url.params['album_name']=='Absent':return httpx.Response(404)
            return httpx.Response(200,json={'id':1,'trackName':'Song','artistName':'Singer','duration':100,'syncedLyrics':'[00:01]Line'})
        return httpx.Response(200,json=[])
    async def run():
        p=Providers(settings(tmp_path),httpx.MockTransport(response))
        first=await p.lyrics({'title':'Song','artist':'Singer','album':'Album'},100)
        assert len(first)==1 and calls==['/api/get']
        second=await p.lyrics({'title':'Song','artist':'Singer','album':'Absent'},100)
        assert second==[] and calls==['/api/get','/api/get','/api/search']
        await p.close()
    asyncio.run(run())


def test_rate_limit_cooldown_and_incomplete_not_no_match(tmp_path):
    calls=[]
    def response(request):
        calls.append(str(request.url))
        if request.url.host=='musicbrainz.org':return httpx.Response(429,headers={'Retry-After':'120'})
        return httpx.Response(200,json=[])
    async def run():
        p=Providers(settings(tmp_path),httpx.MockTransport(response))
        meta={'sha256':'a'*64,'tags':{'title':['Song'],'artist':['Singer']},'duration':100}
        first=await p.analyze(meta,'song.flac')
        assert first['state']=='incomplete' and first['sources'][0]['state']=='rate_limited'
        second=await p.analyze(meta,'song.flac')
        assert second['state']=='incomplete' and second['sources'][0]['state']=='provider_cooldown'
        assert sum('musicbrainz.org' in url for url in calls)==1
        assert sum('lrclib.net' in url for url in calls)==2  # unknown content is transient, not cached
        await p.close()
    asyncio.run(run())


def test_official_musicbrainz_endpoint_is_bounded(tmp_path, monkeypatch):
    for value in ['127.0.0.1', 'musicbrainz.org.evil.example', 'http://musicbrainz.org', 'beta.musicbrainz.org/path']:
        with pytest.raises(ValueError, match='official MusicBrainz'):
            replace(settings(tmp_path), musicbrainz_host=value)
    monkeypatch.setenv('MUSICBRAINZ_HOST', 'beta.musicbrainz.org')
    assert Settings.from_env().musicbrainz_host == 'beta.musicbrainz.org'
    calls=[]
    def response(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'recordings':[]})
    async def run():
        p=Providers(replace(settings(tmp_path), musicbrainz_host='beta.musicbrainz.org'),httpx.MockTransport(response))
        assert await p.musicbrainz({'title':'Song','artist':'Singer'},100)==[]
        await p.close()
    asyncio.run(run())
    assert len(calls)==1 and calls[0].startswith('https://beta.musicbrainz.org/ws/2/recording?')


def test_rate_admission_follows_connection_capacity_without_release_burst(tmp_path,monkeypatch):
    monkeypatch.setitem(providers.INTERVAL,'lrclib',.04)
    async def run():
        first_started=asyncio.Event();release=asyncio.Event();starts=[]
        async def response(request):
            starts.append(time.monotonic())
            if len(starts)==1:
                first_started.set();await release.wait()
            return httpx.Response(200,json=[])
        p=Providers(settings(tmp_path),httpx.MockTransport(response));p.semaphore=asyncio.Semaphore(1)
        tasks=[asyncio.create_task(p.get('lrclib','https://lrclib.net/api/search',{'track':str(index)})) for index in range(3)]
        try:
            await asyncio.wait_for(first_started.wait(),1)
            await asyncio.sleep(.12)  # Previous ordering pre-admitted both waiters.
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks),2)
            assert len(starts)==3 and starts[2]-starts[1]>=.03
            assert db.fetchone(p.settings.db_path,"SELECT COUNT(*) n FROM provider_calls WHERE provider='lrclib'")['n']==3
        finally:
            release.set()
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True);await p.close()
    asyncio.run(run())


def test_overall_deadline_closes_slow_stream_without_hidden_retry(tmp_path,monkeypatch):
    monkeypatch.setattr('phonoteka.providers.GET_DEADLINE_SECONDS',.2)
    async def run():
        class SlowStream(httpx.AsyncByteStream):
            def __init__(self):self.chunks=0;self.closed=False
            async def __aiter__(self):
                for _ in range(100):
                    await asyncio.sleep(.01);self.chunks+=1;yield b' '
            async def aclose(self):self.closed=True
        stream=SlowStream();calls=[]
        def response(request):calls.append(request);return httpx.Response(200,stream=stream)
        p=Providers(settings(tmp_path),httpx.MockTransport(response))
        try:
            with pytest.raises(ProviderError) as exc:
                await asyncio.wait_for(p.get('lrclib','https://lrclib.net/api/search'),2)
            assert exc.value.code=='unknown_outcome'
            assert len(calls)==1 and 0<stream.chunks<100 and stream.closed
            assert db.fetchone(p.settings.db_path,"SELECT COUNT(*) n FROM provider_calls WHERE provider='lrclib'")['n']==1
        finally:await p.close()
    asyncio.run(run())


def test_wire_interval_survives_delayed_budget_database_admission(tmp_path,monkeypatch):
    monkeypatch.setitem(providers.INTERVAL,'lrclib',.04)
    async def run():
        entered=threading.Event();release=threading.Event();starts=[]
        def response(_request):starts.append(time.monotonic());return httpx.Response(200,json=[])
        p=Providers(settings(tmp_path),httpx.MockTransport(response));reserve=p.reserve
        def stalled(*args):
            entered.set()
            assert release.wait(2)
            return reserve(*args)
        p.reserve=stalled
        tasks=[asyncio.create_task(p.get('lrclib','https://lrclib.net/api/search',{'track':str(index)})) for index in range(2)]
        try:
            assert await asyncio.to_thread(entered.wait,1)
            await asyncio.sleep(.12)  # Longer than two admission intervals.
            release.set();await asyncio.wait_for(asyncio.gather(*tasks),2)
            assert len(starts)==2 and starts[1]-starts[0]>=.03
        finally:
            release.set()
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True);await p.close()
    asyncio.run(run())


def test_slow_provider_waiters_leave_global_slots_for_another_source(tmp_path,monkeypatch):
    monkeypatch.setitem(providers.INTERVAL,'musicbrainz',0);monkeypatch.setitem(providers.INTERVAL,'lrclib',0)
    async def run():
        started=asyncio.Event();release=asyncio.Event();seen=[]
        async def response(request):
            seen.append(request.url.host)
            if request.url.host=='musicbrainz.org':
                started.set();await release.wait();return httpx.Response(200,json={'recordings':[]})
            return httpx.Response(200,json=[])
        p=Providers(settings(tmp_path),httpx.MockTransport(response));p.semaphore=asyncio.Semaphore(2)
        waiting=[asyncio.create_task(p.get('musicbrainz','https://musicbrainz.org/ws/2/recording',{'query':str(index)})) for index in range(3)]
        try:
            await asyncio.wait_for(started.wait(),1);await asyncio.sleep(.05)
            assert await asyncio.wait_for(p.get('lrclib','https://lrclib.net/api/search'),.5)==[]
            assert seen==['musicbrainz.org','lrclib.net']
            release.set();await asyncio.wait_for(asyncio.gather(*waiting),2)
        finally:
            release.set()
            for task in waiting:task.cancel()
            await asyncio.gather(*waiting,return_exceptions=True);await p.close()
    asyncio.run(run())
