import asyncio
import httpx
import pytest
from phonoteka.providers import Providers, ProviderError, album_candidates
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


def test_invalid_exact_lyrics_do_not_hide_valid_structured_variant(tmp_path):
    calls = []
    base = {'trackName': 'Song', 'artistName': 'Singer', 'albumName': 'Album', 'duration': 100}

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == '/api/get':
            return httpx.Response(200, json={**base, 'id': 1, 'syncedLyrics': '[02:00]Wrong'})
        return httpx.Response(200, json=[{**base, 'id': 2, 'syncedLyrics': '[00:01]Line'}])

    async def run():
        provider = Providers(settings(tmp_path), httpx.MockTransport(respond))
        try:
            values = await provider.lyrics({'title': 'Song', 'artist': 'Singer', 'album': 'Album'}, 100)
            assert calls == ['/api/get', '/api/search']
            assert [value['id'] for value in values] == ['lrclib:2', 'lrclib:1']
            assert not values[0]['timing_verified']
        finally:
            await provider.close()

    asyncio.run(run())


def test_alternate_lyrics_query_does_not_repeat_legacy_query(tmp_path):
    base = {'trackName': 'Song', 'artistName': 'Singer & Guest', 'albumName': 'Album', 'duration': 100}

    def respond(request):
        assert request.url.path == '/api/search'
        assert dict(request.url.params) == {'q': 'song singer guest'}
        return httpx.Response(200, json=[{**base, 'id': 2, 'syncedLyrics': '[00:01]Line'}])

    async def run():
        provider = Providers(settings(tmp_path), httpx.MockTransport(respond))
        try:
            values = await provider.lyrics({'title': 'Song', 'artist': 'Singer · Guest', 'album': 'Album'},
                                           100, expanded_only=True)
            assert [value['id'] for value in values] == ['lrclib:2']
            assert values[0]['match']['state'] == 'matching_metadata'
            assert values[0]['timing_verified'] is False
        finally:
            await provider.close()

    asyncio.run(run())


def test_album_search_ranks_front_art_from_linked_official_editions(tmp_path):
    recording = '33333333-3333-4333-8333-333333333333'
    releases = [{'id': f'00000000-0000-0000-0000-{i:012d}', 'title': 'Album', 'status': 'Official'}
                for i in range(16)]
    linked = {**releases[-1], 'cover-art-archive': {'front': True}}
    used = []

    async def run():
        provider = Providers(settings(tmp_path))

        async def get(source, url, params=None):
            if url.endswith('/recording'):
                return {'recordings': [{'id': recording, 'title': 'Song', 'length': 100000,
                                        'artist-credit': [{'name': 'Singer'}], 'releases': releases[:-1]}]}
            assert params == {'recording': recording, 'fmt': 'json', 'status': 'official', 'limit': 25}
            return {'release-count': 16, 'releases': [*releases[:-1], linked]}

        async def cover(identity):
            used.append(identity)
            return {'id': 'caa:' + identity, 'kind': 'cover', 'provider': 'coverartarchive',
                    'match': {'state': 'needs_review'}} if identity == linked['id'] else None

        provider.get = get
        provider.cover = cover
        try:
            result = await provider.analyze({'sha256': 'a' * 64, 'duration': 100,
                'tags': {'title': 'Song', 'artist': 'Singer', 'album': 'Album'}}, 'song.mp3', album_hint='Album')
            assert used[0] == linked['id']
            assert len(used) == 3
            assert [c['id'] for c in result['candidates'] if c['kind'] == 'cover'] == ['caa:' + linked['id']]
            assert result['automatic_apply'] is False
        finally:
            await provider.close()

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


@pytest.mark.parametrize('embedded_title,embedded_artist,expected_album',[
    ('Song','Singer','Owned album'),('Different','Singer','Fingerprint edition'),('Song','','Fingerprint edition')])
def test_known_album_is_kept_only_when_embedded_identity_matches(tmp_path,embedded_title,embedded_artist,expected_album):
    async def run():
        provider=Providers(settings(tmp_path));queries=[]
        async def empty(query,duration):queries.append(dict(query));return []
        provider.musicbrainz=empty;provider.lyrics=empty
        try:
            result=await provider.analyze({'sha256':'a'*64,'tags':{'title':embedded_title,'artist':embedded_artist,'album':'Owned album'},'duration':100},
                'song.mp3',identified={'tags':{'title':'Song','artist':'Singer','album':'Fingerprint edition'}})
            assert result['query']['album']==expected_album
            assert all(q['album']==expected_album and q['title']=='Song' for q in queries)
        finally:await provider.close()
    asyncio.run(run())


def test_album_candidates_are_bounded_and_do_not_replace_song_identity():
    releases=[{'id':f'00000000-0000-0000-0000-{i:012d}','title':'Album','date':'2001','status':'Official'} for i in range(12)]
    values=album_candidates([{'releases':releases+releases+[{'id':'../unsafe','title':'Album'}]}],'Album')
    assert len(values)==8 and len({v['id'] for v in values})==8
    assert all(v['kind']=='release' and set(v['tags'])=={'album','musicbrainz_albumid','date'} and v['match']['state']=='needs_review' for v in values)
    assert album_candidates([{'releases':releases}],'Other album')==[]


@pytest.mark.parametrize('ambiguous',[False,True])
def test_missing_album_can_search_cover_only_for_one_unambiguous_album_name(tmp_path,ambiguous):
    async def run():
        provider=Providers(settings(tmp_path));calls=[]
        async def musicbrainz(query,duration):
            releases=[{'id':f'00000000-0000-0000-0000-{i:012d}','title':'Album'} for i in range(9)]
            if ambiguous:releases[-1]['title']='Other album'
            return [{'id':'mb:recording','kind':'metadata','provider':'musicbrainz','match':{'state':'matching_metadata'},'releases':releases}]
        async def lyrics(query,duration):return []
        async def cover(identity):calls.append(identity);return None
        provider.musicbrainz=musicbrainz;provider.lyrics=lyrics;provider.cover=cover
        try:
            result=await provider.analyze({'sha256':'a'*64,'tags':{'title':'Song','artist':'Singer'},'duration':100},'song.mp3')
            assert len(calls)==(0 if ambiguous else 3)
            assert len([c for c in result['candidates'] if c['kind']=='release'])==8
            assert not result['automatic_apply']
        finally:await provider.close()
    asyncio.run(run())
