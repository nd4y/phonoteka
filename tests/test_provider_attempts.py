import asyncio
from dataclasses import replace
import hashlib
import json
import time

import httpx
import pytest

from phonoteka import db,provider_attempts,providers
from phonoteka.providers import Providers,ProviderError
from test_backend import settings,client,authenticated


def test_twenty_concurrent_files_share_two_spent_attempts_across_timeout_and_restart(settings,monkeypatch):
    db.initialize(settings.db_path);cfg=replace(settings,provider_daily_limit=2)
    monkeypatch.setitem(providers.INTERVAL,'lrclib',0)
    calls=[]
    def timeout(request):calls.append(request);raise httpx.ReadTimeout('private-query-and-network-detail')
    async def run():
        p=Providers(cfg,httpx.MockTransport(timeout))
        async def one(index):
            with provider_attempts.input_revision(hashlib.sha256(str(index).encode()).hexdigest()):
                try:await p.get('lrclib','https://lrclib.net/api/search',{'track_name':'private-track-'+str(index)})
                except ProviderError as exc:return exc.code
        outcomes=await asyncio.gather(*(one(index) for index in range(20)))
        await p.close()
        assert outcomes.count('unknown_outcome')==2 and outcomes.count('budget_exhausted')==18
        # Recreating the HTTP client/scheduler grants no new allowance and never
        # resubmits the two unknown requests.
        restarted=Providers(cfg,httpx.MockTransport(timeout))
        try:
            with pytest.raises(ProviderError) as exc:await restarted.get('lrclib','https://lrclib.net/api/search',{'track_name':'after-restart'})
            assert exc.value.code=='budget_exhausted'
        finally:await restarted.close()
    asyncio.run(run())
    assert len(calls)==2
    assert db.fetchone(cfg.db_path,'SELECT COUNT(*) n FROM provider_calls')['n']==2
    report=provider_attempts.report(cfg)
    assert report['total']==2 and {item['state'] for item in report['items']}=={'unknown_outcome'}
    assert all(item['budget_spent'] and item['input_kind']=='file_sha256' and len(item['input_sha256'])==64 for item in report['items'])
    raw=''.join(row['value'] for row in db.fetchall(cfg.db_path,"SELECT value FROM settings WHERE key LIKE 'provider_attempt:%'"))
    assert all(value not in raw for value in ('private-track','private-query','https://','after-restart'))


def test_sent_crash_reconciles_unknown_without_refund_or_resend(settings):
    db.initialize(settings.db_path);cfg=replace(settings,provider_daily_limit=1)
    attempt=provider_attempts.reserve(cfg,'musicbrainz','metadata','a'*64,'file_sha256')
    provider_attempts.mark_sent(cfg,attempt['id'])
    provider_attempts.recover_stale(cfg,now=attempt['reserved_at']+provider_attempts.STALE_SECONDS+1)
    value=json.loads(db.setting(cfg.db_path,provider_attempts.PREFIX+attempt['id']))
    assert value['state']=='unknown_outcome' and value['code']=='executor_lost' and value['budget_spent']
    provider_attempts.finish(cfg,attempt['id'],'succeeded','response_received',200)
    assert json.loads(db.setting(cfg.db_path,provider_attempts.PREFIX+attempt['id']))['state']=='unknown_outcome'
    with pytest.raises(provider_attempts.UsageError,match='budget_exhausted'):provider_attempts.reserve(cfg,'musicbrainz')
    assert db.fetchone(cfg.db_path,'SELECT COUNT(*) n FROM provider_calls')['n']==1


def test_attempt_and_budget_are_one_transaction_and_unfinished_records_are_not_evicted(settings,monkeypatch):
    db.initialize(settings.db_path)
    def failed_record(_value):raise OSError('synthetic attempt journal failure')
    with monkeypatch.context() as patch:
        patch.setattr(provider_attempts.db,'encode',failed_record)
        with pytest.raises(OSError):provider_attempts.reserve(settings,'musicbrainz')
    assert db.fetchone(settings.db_path,'SELECT COUNT(*) n FROM provider_calls')['n']==0
    monkeypatch.setattr(provider_attempts,'MAX_RECORDS',1)
    attempt=provider_attempts.reserve(settings,'musicbrainz')
    with pytest.raises(provider_attempts.UsageError,match='attempt_ledger_full'):provider_attempts.reserve(settings,'lrclib')
    assert json.loads(db.setting(settings.db_path,provider_attempts.PREFIX+attempt['id']))['state']=='reserved'
    assert db.fetchone(settings.db_path,'SELECT COUNT(*) n FROM provider_calls')['n']==1


def test_cache_hit_has_no_http_attempt_and_attempt_api_is_private_bounded_and_content_free(client,settings):
    assert client.get('/api/reports/provider-attempts').status_code==401
    headers=authenticated(client,settings)
    db.set_setting(settings.db_path,'provider_usage:lrclib:lyrics',db.encode({'allow_cache':True,'cache_ttl':3600,'basis':'Explicit cache grant for ledger fixture'}))
    async def run():
        p=Providers(settings,httpx.MockTransport(lambda _request:httpx.Response(200,json=[])))
        try:
            await p.get('lrclib','https://lrclib.net/api/search',{'track_name':'private-cached-title'})
            await p.get('lrclib','https://lrclib.net/api/search',{'track_name':'private-cached-title'})
        finally:await p.close()
    asyncio.run(run())
    response=client.get('/api/reports/provider-attempts?page_size=1')
    assert response.status_code==200 and response.json()['total']==1 and len(response.json()['items'])==1
    item=response.json()['items'][0]
    assert item['state']=='no_match' and item['http_status']==200 and item['input_kind']=='unknown'
    assert 'private-cached-title' not in response.text and 'executor' not in response.text and 'budget_reservation' not in response.text
    assert response.headers['cache-control']=='private, no-store'
    assert client.get('/api/reports/provider-attempts?page_size=101').status_code==422
    assert client.get('/api/reports/provider-attempts?state=private').status_code==422
    assert client.get('/api/v1/reports/provider-attempts').status_code==200
