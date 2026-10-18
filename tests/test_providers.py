import asyncio
from dataclasses import replace
import json
from pathlib import Path
import httpx
import pytest
from phonoteka import db
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
        p=Providers(settings(tmp_path),httpx.MockTransport(response))
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
        assert len(calls)==2
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
