import asyncio
import io
import json
import subprocess
import wave
from dataclasses import replace

import httpx
import pytest

from phonoteka import db, media, recognition
from phonoteka.providers import Providers, ProviderError
from test_providers import settings


def test_real_fingerprint_contains_no_audio_or_private_metadata(tmp_path):
    cfg=settings(tmp_path)
    for root in cfg.roots.values():root.mkdir()
    path=cfg.work_root/'private name.wav'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=15',str(path)],check=True)
    before=media.sha256_file(path)
    audio,offset=recognition.sample(path,cfg.roots,15)
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnframes()<=192000 and wav.getframerate()==16000
    calls=[]
    def response(request):
        payload=json.loads(request.content);calls.append(payload)
        assert request.url.host=='amp.shazam.com'
        assert set(payload)=={'timezone','signature','timestamp','context','geolocation'}
        assert payload['signature']['uri'].startswith('data:audio/vnd.shazam.sig;base64,')
        assert payload['signature']['samplems']<=12001
        assert 'private' not in request.content.decode()
        return httpx.Response(200,json={'track':{'key':'1','title':'Known','subtitle':'Artist','url':'https://evil.example/x','sections':[]}})
    async def run():
        p=Providers(cfg,httpx.MockTransport(response))
        first=await recognition.recognize(p,audio,offset)
        second=await recognition.recognize(p,audio,offset)
        assert first==second and len(calls)==1 and first['source_url']==''
        assert first['identification']=='audio_fingerprint'
        assert first['recording_version_verified'] is False
        await p.close()
    asyncio.run(run())
    assert media.sha256_file(path)==before


def test_transport_rejects_extra_payload_hosts_and_redirect(tmp_path):
    async def run():
        p=Providers(settings(tmp_path),httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':'https://evil.example'})))
        transport=recognition.SignatureTransport(p)
        data={'timezone':'Europe/London','signature':{'uri':'data:audio/vnd.shazam.sig;base64,AAAA','samplems':12000},'timestamp':1,'context':{},'geolocation':{}}
        for url,payload in [('https://evil.example/discovery/v5/',data),('https://amp.shazam.com/discovery/v5/',{**data,'filename':'private'})]:
            with pytest.raises(ProviderError,match='Недопустимый'):
                await transport.request('POST',url,json=payload)
        with pytest.raises(ProviderError) as exc: await transport.request('POST','https://amp.shazam.com/discovery/v5/',json=data)
        assert exc.value.code=='provider_unavailable'
        await p.close()
    asyncio.run(run())


def test_rate_limit_one_request_and_cancel_before_send(tmp_path):
    calls=[]
    async def run():
        p=Providers(settings(tmp_path),httpx.MockTransport(lambda r:(calls.append(r) or httpx.Response(429,headers={'Retry-After':'600'}))))
        data={'timezone':'Europe/London','signature':{'uri':'data:audio/vnd.shazam.sig;base64,AAAA','samplems':12000},'timestamp':1,'context':{},'geolocation':{}}
        with pytest.raises(ProviderError) as exc: await recognition.SignatureTransport(p).request('POST','https://amp.shazam.com/discovery/v5/',json=data)
        assert exc.value.code=='rate_limited' and len(calls)==1
        assert json.loads(db.setting(p.settings.db_path,'provider_breaker:shazam'))['reason']=='rate_limited'
        def cancelled(): raise media.MediaError('authorization_revoked','cancelled')
        with pytest.raises(media.MediaError): await recognition.SignatureTransport(p,cancelled).request('POST','https://amp.shazam.com/discovery/v5/',json=data)
        assert len(calls)==1
        await p.close()
    asyncio.run(run())


def test_fingerprint_query_replaces_wrong_embedded_tags(tmp_path):
    queries=[]
    def response(request):
        queries.append(str(request.url))
        return httpx.Response(200,json={'recordings':[]} if request.url.host=='musicbrainz.org' else [])
    async def run():
        p=Providers(settings(tmp_path),httpx.MockTransport(response))
        result=await p.analyze({'sha256':'a'*64,'duration':100,'tags':{'title':'Wrong'}},'123.mp3',{'tags':{'title':'Correct','artist':'Actual'}})
        assert result['query_source']=='audio_fingerprint' and result['query']['title']=='Correct'
        assert all('Wrong' not in query for query in queries)
        await p.close()
    asyncio.run(run())
