import asyncio
import httpx
from phonoteka.providers import Providers, ProviderError
from test_providers import settings


def test_plain_exact_result_does_not_hide_synchronized_variants(tmp_path):
    calls=[]
    def respond(request):
        calls.append(request.url.path)
        base={'trackName':'Song','artistName':'Singer','albumName':'Album','duration':100}
        if request.url.path=='/api/get':return httpx.Response(200,json={**base,'id':1,'plainLyrics':'Line'})
        return httpx.Response(200,json=[{**base,'id':1,'plainLyrics':'Line'},
            {**base,'id':2,'syncedLyrics':'[00:01]Line'}])
    async def run():
        provider=Providers(settings(tmp_path),httpx.MockTransport(respond))
        try:
            values=await provider.lyrics({'title':'Song','artist':'Singer','album':'Album'},100)
            assert calls==['/api/get','/api/search']
            assert [v['id'] for v in values]==['lrclib:2','lrclib:1']
            assert not values[0]['timing_verified']
        finally:await provider.close()
    asyncio.run(run())


def test_failed_fallback_preserves_plain_result_and_retry_reason(tmp_path):
    async def run():
        provider=Providers(settings(tmp_path))
        async def get(service,url,params):
            if url.endswith('/api/get'):return {'id':1,'trackName':'Song','artistName':'Singer','duration':100,'plainLyrics':'Line'}
            raise ProviderError('rate_limited','lrclib','wait')
        provider.get=get
        try:
            values=await provider.lyrics({'title':'Song','artist':'Singer','album':'Album'},100)
            assert values[0]['plain_lyrics']=='Line'
            assert values[0]['search_incomplete']=='rate_limited'
        finally:await provider.close()
    asyncio.run(run())


def test_alternative_editions_have_bounded_reviewable_cover_search(tmp_path):
    async def run():
        provider=Providers(settings(tmp_path));calls=[]
        async def musicbrainz(query,duration):
            return [{'id':'mb:recording','kind':'metadata','provider':'musicbrainz','match':{'state':'matching_metadata'},
                'releases':[{'id':f'00000000-0000-0000-0000-{i:012d}','title':'Album'} for i in range(5)]}]
        async def lyrics(query,duration):return []
        async def cover(identity):
            calls.append(identity)
            return {'id':'caa:'+identity,'kind':'cover','provider':'coverartarchive','match':{'state':'needs_review','reasons':['confirm_release']}}
        provider.musicbrainz=musicbrainz;provider.lyrics=lyrics;provider.cover=cover
        try:
            result=await provider.analyze({'sha256':'a'*64,'tags':{'title':'Song','artist':'Singer','album':'Album'},'duration':100},'song.mp3')
            assert len(calls)==3
            source=next(s for s in result['sources'] if s['provider']=='coverartarchive')
            assert source['matching_releases']==5 and source['attempted_releases']==3
            assert all(c['match']['state']=='needs_review' for c in result['candidates'] if c['kind']=='cover')
            assert not result['automatic_apply']
        finally:await provider.close()
    asyncio.run(run())
