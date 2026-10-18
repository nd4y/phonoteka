import asyncio
from dataclasses import replace
import httpx
from phonoteka.providers import Providers
from test_providers import settings


def test_optional_catalogs_disabled_no_requests(tmp_path):
    calls=[]
    async def run():
        p=Providers(settings(tmp_path),httpx.MockTransport(lambda r:(calls.append(r.url.host) or httpx.Response(200,json={ 'recordings':[]} if 'musicbrainz' in r.url.host else []))))
        await p.analyze({'sha256':'a'*64,'tags':{'title':'Song','artist':'Artist'},'duration':100},'song.flac')
        assert set(calls)=={'musicbrainz.org','lrclib.net'}
        await p.close()
    asyncio.run(run())


def test_release_candidates_preserve_actual_release_and_no_image_download(tmp_path):
    token=tmp_path/'token';token.write_text('test-token-123456789')
    cfg=replace(settings(tmp_path),theaudiodb_enabled=True,discogs_enabled=True,discogs_token_file=token)
    calls=[]
    def response(request):
        calls.append(request)
        if request.url.host=='api.discogs.com':
            assert request.headers['Authorization']=='Discogs token=test-token-123456789' and 'token' not in str(request.url)
            return httpx.Response(200,json={'results':[{'id':42,'title':'Artist - Different edition'}]} if request.url.path.endswith('search') else {'title':'Different edition','year':1999,'tracklist':[{'position':'1','title':'Song'}]})
        if request.url.host=='www.theaudiodb.com':return httpx.Response(200,json={'album':[{'idAlbum':'123','strAlbum':'Album','strArtist':'Artist','intYearReleased':'2000','strAlbumThumb':'http://127.0.0.1/private'}]})
        return httpx.Response(200,json={'recordings':[]} if request.url.host=='musicbrainz.org' else [])
    async def run():
        p=Providers(cfg,httpx.MockTransport(response));result=await p.analyze({'sha256':'a'*64,'tags':{'title':'Song','artist':'Artist','album':'Album'},'duration':100},'song.flac')
        discogs=next(c for c in result['candidates'] if c['provider']=='discogs')
        assert discogs['tags']['album']=='Different edition' and 'title' not in discogs['tags']
        assert discogs['release']['tracklist'][0]['title']=='Song' and discogs['match']['state']=='needs_review'
        assert all(c['kind']!='cover' for c in result['candidates'])
        assert not any('127.0.0.1' in str(r.url) for r in calls)
        await p.close()
    asyncio.run(run())
