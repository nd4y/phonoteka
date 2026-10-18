import asyncio
from dataclasses import replace
import json
import subprocess
import time

import httpx
from fastapi.testclient import TestClient
from mutagen.id3 import ID3, COMM
import pytest

from phonoteka import db, media, youtube
from phonoteka.app import create_app
from phonoteka.providers import Providers, ProviderError
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, job

A='dQw4w9WgXcQ';B='YE7VzlLtp-4'


@pytest.mark.parametrize('url',[
    f'https://youtu.be/{A}?t=5',f'https://www.youtube.com/watch?v={A}&list=abc',
    f'https://music.youtube.com/watch?v={A}',f'https://m.youtube.com/shorts/{A}',
    f'https://www.youtube.com/live/{A}',f'https://www.youtube-nocookie.com/embed/{A}'])
def test_canonical_video_urls(url):assert youtube.video_id(url)==A


@pytest.mark.parametrize('url',[
    f'https://youtube.com.evil.test/watch?v={A}',f'https://youtube.com@evil.test/watch?v={A}',
    f'https://user:password@youtube.com/watch?v={A}',f'file:///watch?v={A}',
    'https://youtube.com/playlist?list=PL123',f'https://youtube.com:555/watch?v={A}',
    'https://youtube.com/watch?v=bad','https://youtube.com:bad/watch?v='+A])
def test_untrusted_urls_rejected(url):assert youtube.video_id(url) is None


def test_source_tags_exclude_description_lyrics_and_arbitrary_references():
    assert youtube.source_tag('TXXX:purl') and youtube.source_tag('COMM::eng') and youtube.source_tag('----:com.apple.iTunes:purl')
    assert not youtube.source_tag('TXXX:description') and not youtube.source_tag('WXXX:related_video') and not youtube.source_tag('LYRICS')


def fixture_audio(cfg,name):
    path=cfg.work_root/name
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2','-c:a','libmp3lame',str(path)],check=True)
    return path


def test_embedded_filename_sidecar_conflict_and_bounds(settings):
    path=fixture_audio(settings,f'Example [{A}].mp3');tags=ID3(path);tags.add(COMM(encoding=3,lang='eng',desc='',text=[youtube.canonical(A)]));tags.save(path)
    path.with_suffix('.info.json').write_text(json.dumps({'id':B,'extractor_key':'Youtube','webpage_url':youtube.canonical(B),'cookies':'never retained'}))
    data=youtube.merge(youtube.discover(path,settings,{}))
    assert data['state']=='conflict' and data['linked_id'] is None
    assert data['candidates'][0]['evidence']==['filename_id','embedded_url']
    assert len(data['candidates'])==2 and 'cookies' not in json.dumps(data)
    path.with_suffix('.info.json').write_text(json.dumps({'id':22,'extractor_key':'Youtube'}))
    data=youtube.merge(youtube.discover(path,settings,{}));assert data['state']=='recorded' and data['linked_id']==A
    path.with_suffix('.info.json').write_bytes(b'x'*(2*1024**2+1))
    assert 'sidecar_too_large' in youtube.discover(path,settings,{})['warnings']


def test_filename_is_candidate_not_proven_origin_and_symlink_not_followed(settings):
    path=fixture_audio(settings,f'Example-{B}.mp3')
    sidecar=path.with_suffix('.info.json');secret=settings.state_dir/'private.json';secret.write_text(json.dumps({'id':A,'extractor_key':'Youtube'}));sidecar.symlink_to(secret)
    data=youtube.merge(youtube.discover(path,settings,{}))
    assert data['state']=='candidates' and not data['linked_id'] and len(data['candidates'])==1
    assert 'sidecar_unreadable' in data['warnings']


def test_derivative_inherits_source_link(settings):
    path=fixture_audio(settings,'source.mp3');tags=ID3(path);tags.add(COMM(encoding=3,lang='eng',text=[youtube.canonical(A)]));tags.save(path)
    derived=fixture_audio(settings,'derived.mp3')
    meta={'derived_from':{'source':'source.mp3','source_sha256':media.sha256_file(path)}}
    result=youtube.merge(youtube.discover(derived,settings,meta))
    assert result['linked_id']==A and result['candidates'][0]['inherited']
    path.write_bytes(path.read_bytes()+b'changed')
    assert not youtube.discover(derived,settings,meta)['candidates']


def test_owner_link_unlink_survives_rescan_and_stale_put_rejected(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as c:
        headers=authenticated(c,settings);tid,sha=seed(settings)
        route='/api/tracks/'+tid+'/youtube'
        assert c.put(route,json={'url':youtube.canonical(A)},headers=headers).status_code==409
        headers={**headers,'If-Match':sha}
        assert c.put(route,json={'url':'https://evil.test/'+A},headers=headers).status_code==422
        response=c.put(route,json={'url':youtube.canonical(A)},headers=headers);assert response.status_code==200
        data=response.json();assert data['state']=='owner_confirmed'
        assert youtube.merge({'candidates':[]},data)['linked_id']==A
        data=c.put(route,json={'url':None},headers=headers).json()
        assert youtube.merge({'candidates':[{'video_id':A,'evidence':['embedded_url']}]},data)['state']=='unlinked'
        assert media.sha256_file(settings.work_root/'a.mp3')==sha
        assert c.get('/api/reports/export/json').json()['files'][0]['youtube_state']=='unlinked'


def test_oembed_available_is_not_confirmation_and_search_uses_fingerprint(settings,monkeypatch):
    cfg=replace(settings,youtube_enabled=True);db.initialize(cfg.db_path);queries=[]
    async def search(q):
        queries.append(q);return [{'id':A,'title':'Music video','channel':'Uploader','duration':202},{'id':'invalid'}]
    monkeypatch.setattr(youtube,'flat_search',search)
    async def run():
        p=Providers(cfg,httpx.MockTransport(lambda r:httpx.Response(200,json={'title':'A video','author_name':'Channel','html':'<script>never render</script>'})))
        local=youtube.merge({'candidates':[{'video_id':B,'url':youtube.canonical(B),'evidence':['filename_id']}]})
        available=await youtube.enrich(p,local,{},'x.mp3',{})
        assert available['candidates'][0]['availability']=='available' and not available['linked_id']
        assert 'html' not in json.dumps(available)
        p.next.clear()
        analysis={'candidates':[{'provider':'shazam','tags':{'title':'Correct','artist':'Artist'}}]}
        data=await youtube.enrich(p,youtube.merge({'candidates':[]}),{'duration':200,'tags':{'title':'Wrong'}},'wrong.mp3',analysis,True)
        assert queries==['Artist Correct'] and data['linked_id'] is None
        assert len(data['candidates'])==1 and data['candidates'][0]['duration_delta']==2
        assert data['query_source']=='audio_fingerprint' and data['state']=='candidates'
        await p.close()
    asyncio.run(run())


def test_unavailable_video_preserves_recorded_provenance(settings):
    cfg=replace(settings,youtube_enabled=True);db.initialize(cfg.db_path)
    async def run():
        p=Providers(cfg,httpx.MockTransport(lambda r:httpx.Response(404)))
        data=youtube.merge({'candidates':[{'video_id':A,'url':youtube.canonical(A),'evidence':['embedded_url']}]})
        result=await youtube.enrich(p,data,{},'x',{})
        assert result['linked_id']==A and result['candidates'][0]['availability']=='unavailable'
        await p.close()
    asyncio.run(run())


def test_local_job_external_opt_in_and_durable_retry(settings,monkeypatch):
    cfg=replace(settings,youtube_enabled=True)
    async def unavailable(*args,**kwargs):raise ProviderError('provider_unavailable','youtube','temporary')
    monkeypatch.setattr(youtube,'enrich',unavailable)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as c:
        headers=authenticated(c,cfg);tid,sha=seed(cfg)
        queued=job(c,headers,'youtube',[tid]);worker=Worker(cfg)
        asyncio.run(worker.process(worker.claim_job()));assert c.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        queued=job(c,headers,'youtube',[tid],{'youtube_lookup':True,'youtube_search':True})
        asyncio.run(worker.process(worker.claim_job()));state=c.get('/api/jobs/'+queued['id']).json()
        assert state['state']=='queued' and state['retry_at']>time.time() and state['completed']==0
        assert c.get('/api/tracks/'+tid).json()['youtube']['lookup_error']=='provider_unavailable'
        worker.pool.shutdown();asyncio.run(worker.providers.close())
    with TestClient(create_app(settings),base_url=settings.app_url) as c:
        headers=authenticated(c,settings)
        assert c.post('/api/jobs',json={'kind':'youtube','track_ids':[tid],'params':{'youtube_lookup':True}},headers={**headers,'Idempotency-Key':'off'}).status_code==409


def test_live_flat_search_adapter_only_metadata(monkeypatch):
    # The real yt-dlp executable/YouTube contract is checked separately in deployment.
    class Stream:
        async def read(self,n):
            if getattr(self,'done',False):return b''
            self.done=True;return json.dumps({'entries':[{'id':A}]}).encode()
    class Process:
        returncode=0;stdout=Stream()
        async def wait(self):return 0
    captured=[]
    async def spawn(*args,**kwargs):captured.extend(args);return Process()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    assert asyncio.run(youtube.flat_search('Artist Song'))==[{'id':A}]
    assert '--skip-download' in captured and '--flat-playlist' in captured and '--no-cookies' in captured and captured[-1]=='ytsearch3:Artist Song'
