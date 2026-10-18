"""REC-02/09: real durable admission, mocked network, generated audio only."""
import asyncio
from dataclasses import replace
import hashlib
import json
import shutil
import subprocess
import time

import httpx
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
import pytest

from phonoteka import db, media, provider_attempts, providers
from phonoteka.app import create_app
from phonoteka.providers import Providers, ProviderError
from test_backend import settings, authenticated, job, drain


def usage(cfg,identity):
    return json.loads(db.setting(cfg.db_path,provider_attempts.RECOGNITION_PREFIX+identity))


def test_twenty_concurrent_http_attempts_share_three_spent_unknown_calls(settings):
    db.initialize(settings.db_path);calls=[];identity='a'*64
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('Synthetic response loss after send')
    async def run():
        # Independent adapters/locks deliberately contend on the durable shared
        # admission transaction rather than accidentally serializing one client.
        clients=[Providers(settings,httpx.MockTransport(timeout)) for _ in range(20)]
        async def request(index):
            try:
                with provider_attempts.recognition_scope(identity,'batch-'+str(index)):
                    async with clients[index].request('shazam','fingerprint','POST','https://amp.shazam.com/discovery/v5/',
                            revision=hashlib.sha256(str(index).encode()).hexdigest(),input_kind='file_sha256'):
                        return 'unexpected_success'
            except ProviderError as error:return error.code
        try:return await asyncio.gather(*(request(index) for index in range(20)))
        finally:await asyncio.gather(*(client.close() for client in clients))
    outcomes=asyncio.run(run())
    assert outcomes.count('unknown_outcome')==3 and outcomes.count('recognition_attempt_budget_exhausted')==17
    assert len(calls)==3 and usage(settings,identity)['calls']==3
    assert db.fetchone(settings.db_path,'SELECT COUNT(*) n FROM provider_calls')['n']==3
    report=provider_attempts.report(settings)
    assert {attempt['state'] for attempt in report['items']}=={'unknown_outcome'}
    assert all(attempt['budget_spent'] and attempt['audio_identity']==identity for attempt in report['items'])
    assert report['recognition_budget']['spent_calls']==3 and report['recognition_budget']['exhausted_identities']==1


def test_reserved_sent_unknown_and_future_fallback_share_identity_without_batch_or_day_reset(settings,monkeypatch):
    db.initialize(settings.db_path);identity='b'*64
    with provider_attempts.recognition_scope(identity,'first-batch'):
        reserved=provider_attempts.reserve(settings,'shazam','fingerprint','1'*64,'file_sha256')
        sent=provider_attempts.reserve(settings,'future-fallback','fingerprint','2'*64,'file_sha256')
        provider_attempts.mark_sent(settings,sent['id'])
        third=provider_attempts.reserve(settings,'shazam','fingerprint','3'*64,'file_sha256')
        provider_attempts.mark_sent(settings,third['id'])
        provider_attempts.finish(settings,third['id'],'unknown_outcome','network_outcome_unknown')
    assert json.loads(db.setting(settings.db_path,provider_attempts.PREFIX+reserved['id']))['state']=='reserved'
    assert json.loads(db.setting(settings.db_path,provider_attempts.PREFIX+sent['id']))['state']=='sent'
    # Both daily reservations and the bounded attempt journal can age out. The
    # independent content quota must survive that cleanup and a new scheduler.
    advanced=time.time()+provider_attempts.RETENTION_SECONDS+provider_attempts.STALE_SECONDS+10
    provider_attempts.recover_stale(settings,now=advanced)
    provider_attempts.recover_stale(settings,now=advanced+provider_attempts.RETENTION_SECONDS+1)
    monkeypatch.setattr(provider_attempts.time,'time',lambda:advanced+provider_attempts.RETENTION_SECONDS+2)
    provider_attempts.reserve(settings,'musicbrainz','metadata')
    assert db.fetchone(settings.db_path,"SELECT COUNT(*) n FROM provider_calls WHERE provider<>'musicbrainz'")['n']==0
    with provider_attempts.recognition_scope(identity,'new-job-new-batch'):
        with pytest.raises(provider_attempts.UsageError,match='recognition_attempt_budget_exhausted'):
            provider_attempts.reserve(settings,'another-fallback','fingerprint','4'*64,'file_sha256')
    assert usage(settings,identity)['calls']==3


def test_new_batch_does_not_reset_daily_budget_and_metadata_is_outside_three_call_cap(settings):
    db.initialize(settings.db_path);cfg=replace(settings,provider_daily_limit=4)
    identity='c'*64
    with provider_attempts.recognition_scope(identity,'one'):
        for _ in range(3):provider_attempts.reserve(cfg,'shazam','fingerprint')
    with provider_attempts.recognition_scope('d'*64,'two'):
        provider_attempts.reserve(cfg,'shazam','fingerprint')
    with provider_attempts.recognition_scope('e'*64,'three'):
        with pytest.raises(provider_attempts.UsageError,match='budget_exhausted'):
            provider_attempts.reserve(cfg,'shazam','fingerprint')
    assert not db.setting(cfg.db_path,provider_attempts.RECOGNITION_PREFIX+'e'*64)
    # MusicBrainz catalogue lookups are metadata, not acoustic recognition.
    for _ in range(4):provider_attempts.reserve(cfg,'musicbrainz','metadata',identity,'file_sha256')
    assert usage(cfg,identity)['calls']==3
    with pytest.raises(provider_attempts.UsageError,match='recognition_identity_required'):
        provider_attempts.reserve(cfg,'shazam','fingerprint',identity,'file_sha256')


def test_tag_only_change_and_new_job_do_not_grant_new_recognition_allowance(settings):
    cfg=replace(settings,external_fingerprints=True,external_metadata=False)
    path=cfg.work_root/'quota.flac'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','sine=frequency=440:duration=4',
                    '-metadata','title=Original','-c:a','flac',str(path)],check=True)
    shutil.copy2(path,cfg.source_root/path.name)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        identity=media.audio_identity(path);before=media.sha256_file(path)
        with provider_attempts.recognition_scope(identity,'before-tags'):
            for _ in range(3):provider_attempts.reserve(cfg,'shazam','fingerprint',before,'file_sha256')
        audio=FLAC(path);audio['title']=['Only the tags changed'];audio.save()
        after=media.sha256_file(path)
        assert before!=after and media.audio_identity(path)==identity
        job(client,headers,'scan');drain(cfg)
        track=client.get('/api/tracks').json()['items'][0]
        queued=job(client,headers,'recognize',[track['id']]);drain(cfg)
        result=client.get('/api/jobs/'+queued['id']).json()
        assert result['state']=='needs_review',result
        assert result['results'][0]['code']=='recognition_attempt_budget_exhausted'
        assert result['stage']=='needs_review' and result['retry_at'] is None
        assert usage(cfg,identity)['calls']==3
