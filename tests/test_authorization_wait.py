"""Grant loss stops admission, not the identity or history of an accepted job."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import sys
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from phonoteka import automation, automatic_quality, db, inference_client, media, providers, scheduling, job_activity
from phonoteka.metrics import DatabaseCollector
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, job as enqueue_owner, drain, synthetic
from test_token_scopes import issue, enqueue


def current(client, identity):
    response=client.get('/api/jobs/'+identity)
    assert response.status_code==200,response.text
    return response.json()


def approve(client, owner, state, scopes=None):
    return client.post('/api/jobs/'+state['id']+'/reauthorize',headers=owner,
        json={'scopes':state['authorization']['required_scopes'] if scopes is None else scopes,
              'authorization_version':state['authorization']['version']})


@pytest.mark.parametrize('change',['revoke','expire','narrow'])
@pytest.mark.parametrize('critical_started',[False,True])
def test_grant_loss_drains_only_begun_effects_then_reapproves_same_remaining_job(settings, change, critical_started):
    cfg=replace(settings,file_workers=1,io_threads=1)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        owner=authenticated(client,cfg)
        ids=[seed(cfg,f'{index}.mp3')[0] for index in range(4)]
        token, auth=issue(client,owner,['jobs:create'])
        queued=enqueue(client,auth,'audit',ids).json();identity=queued['id']
        initial=json.loads(db.fetchone(cfg.db_path,'SELECT payload FROM jobs WHERE id=?',(identity,))['payload'])
        entered=threading.Event();release=threading.Event();effects=Counter()
        worker=Worker(cfg)
        def operation(job,item):
            if critical_started:worker.authorize_grant(job)
            entered.set()
            assert release.wait(10),'bounded operation barrier'
            if not critical_started:worker.authorize_grant(job)
            effects[item['track_id']]+=1
            return {'effect_committed':True}
        worker.process_item=operation
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future=executor.submit(lambda:asyncio.run(worker.process(worker.claim_job())))
                try:
                    assert entered.wait(10),'first admitted item did not enter'
                    if change=='revoke':
                        assert client.delete('/api/tokens/'+token['id'],headers=owner).status_code==200
                    else:
                        with db.transaction(cfg.db_path) as conn:
                            if change=='expire':conn.execute('UPDATE api_tokens SET expires=? WHERE id=?',(time.time()-1,token['id']))
                            else:conn.execute('UPDATE api_tokens SET scopes=? WHERE id=?',(db.encode(['library:read']),token['id']))
                    assert not effects
                finally:release.set()
                future.result(timeout=10)
            waiting=current(client,identity)
            assert waiting['state']=='waiting_authorization' and not waiting['cancel_requested']
            assert waiting['completed']==int(critical_started) and waiting['failed']==0
            assert waiting['remaining']==4-int(critical_started)
            assert waiting['eta_seconds'] is None and waiting['eta_reason']=='waiting_authorization'
            assert waiting['authorization']['reason']=={'revoke':'authorization_revoked','expire':'authorization_expired','narrow':'authorization_scope_denied'}[change]
            assert worker.claim_job() is None
            assert client.post('/api/jobs/'+identity+'/resume',headers=owner).json()['code']=='authorization_required'
            # Even a different, fully powered bearer cannot issue an owner grant.
            _, powerful=issue(client,owner,sorted(automation.CANONICAL_SCOPES))
            assert approve(client,powerful,waiting).status_code==403
            assert approve(client,owner,waiting,['jobs:create','files:write']).status_code==422
            accepted=approve(client,owner,waiting)
            assert accepted.status_code==200,accepted.text
            assert accepted.json()['id']==identity and accepted.json()['completed']==waiting['completed']
            payload=json.loads(db.fetchone(cfg.db_path,'SELECT payload FROM jobs WHERE id=?',(identity,))['payload'])
            assert payload['items']==initial['items'] and payload['params']==initial['params']
            assert payload['auth_limited'] is True and payload['auth_scopes']==['jobs:create']
            assert 'auth_token_id' not in payload and 'authorization_wait' not in payload
            assert approve(client,owner,waiting).status_code==409
            asyncio.run(worker.process(worker.claim_job()))
            done=current(client,identity)
            assert done['state']=='succeeded' and done['completed']==4 and done['failed']==0
            assert effects==Counter({identity:1 for identity in ids})
            assert len(done['results'])==4
            events=db.fetchall(cfg.db_path,"SELECT payload FROM events WHERE kind='job_reauthorized'")
            assert len(events)==1 and json.loads(events[0]['payload'])['previous_grant']['token_id']==token['id']
            assert db.fetchone(cfg.db_path,'SELECT revoked,expires,scopes FROM api_tokens WHERE id=?',(token['id'],))!=None
        finally:worker.pool.shutdown()


def test_pause_user_cancel_epoch_and_logout_remain_separate_from_owner_grant(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        owner=authenticated(client,settings);tid,_=seed(settings)
        token,auth=issue(client,owner,['jobs:create'])
        identity=enqueue(client,auth,'audit',[tid]).json()['id']
        assert client.post('/api/jobs/'+identity+'/pause',headers=auth).status_code==200
        assert client.delete('/api/tokens/'+token['id'],headers=owner).status_code==200
        waiting=current(client,identity)
        assert waiting['pause_requested']
        approved=approve(client,owner,waiting)
        assert approved.status_code==200 and approved.json()['state']=='paused'
        assert client.post('/api/jobs/'+identity+'/resume',headers=owner).status_code==200
        worker=Worker(settings)
        try:
            before=db.fetchone(settings.db_path,'SELECT * FROM jobs WHERE id=?',(identity,))
            assert client.post('/api/auth/logout',headers=owner).status_code==200
            assert not worker.cancelled(identity),'ordinary logout must not revoke a job grant'
            owner=authenticated(client,settings)
            db.set_setting(settings.db_path,'auth_epoch',str(before['auth_epoch']+1))
            assert worker.claim_job() is None
            waiting=db.fetchone(settings.db_path,'SELECT * FROM jobs WHERE id=?',(identity,))
            assert waiting['state']=='waiting_authorization' and waiting['auth_epoch']==before['auth_epoch']
            assert client.post('/api/jobs/'+identity+'/reauthorize',headers=owner,
                json={'scopes':['jobs:create'],'authorization_version':json.loads(waiting['payload'])['authorization_version']}).status_code==401
            owner=authenticated(client,settings)
            state=current(client,identity)
            stale={**state,'authorization':{**state['authorization'],'version':state['authorization']['version']-1}}
            assert approve(client,owner,stale).json()['code']=='authorization_version_conflict'
            assert approve(client,owner,state).status_code==200
            token2,auth2=issue(client,owner,['jobs:create'])
            second=enqueue(client,auth2,'audit',[tid]).json()['id']
            assert client.delete('/api/tokens/'+token2['id'],headers=owner).status_code==200
            cancelled=client.post('/api/jobs/'+second+'/cancel',headers=owner).json()
            assert cancelled['state']=='cancelled' and cancelled['cancel_requested']
            assert approve(client,owner,cancelled).status_code==409
            assert db.fetchone(settings.db_path,'SELECT revoked FROM api_tokens WHERE id=?',(token2['id'],))['revoked']==1
        finally:worker.pool.shutdown()


def test_limited_owner_grant_cannot_be_forged_or_amplified_for_automatic_apply(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        owner=authenticated(client,settings);tid,_=seed(settings)
        for params in ({'auth_limited':False},{'auth_scopes':['files:write']},{'authorization_wait':None}):
            assert enqueue(client,owner,'audit',[tid],params).status_code==422
        epoch=int(db.setting(settings.db_path,'auth_epoch','0'))
        with db.connect(settings.db_path) as conn:
            assert automatic_quality.actor_snapshot(conn,{'auth_epoch':epoch})['write_authorized']
            for scopes in (['jobs:create'],None,['files:write',42]):
                assert not automatic_quality.actor_snapshot(conn,{'auth_epoch':epoch,'auth_limited':True,'auth_scopes':scopes})['write_authorized']
            assert automatic_quality.actor_snapshot(conn,{'auth_epoch':epoch,'auth_limited':True,'auth_scopes':['jobs:create','files:write']})['write_authorized']


def test_wait_blocks_schedule_duplicates_and_keeps_pending_retry_and_progress(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        owner=authenticated(client,settings);tid,_=seed(settings)
        token,auth=issue(client,owner,['jobs:create'])
        identity=enqueue(client,auth,'audit',[tid]).json()['id'];retry=time.time()+600
        with db.transaction(settings.db_path) as conn:
            payload=json.loads(conn.execute('SELECT payload FROM jobs WHERE id=?',(identity,)).fetchone()['payload'])
            payload.update(retry_at=retry,wait_reason='provider')
            conn.execute("UPDATE jobs SET payload=?,stage='waiting_provider' WHERE id=?",(db.encode(payload),identity))
        assert client.delete('/api/tokens/'+token['id'],headers=owner).status_code==200
        schedule={'enabled':True,'auth_epoch':int(db.setting(settings.db_path,'auth_epoch','0')),'next_run':0,
                  'steps':['scan'],'remaining':[],'interval_minutes':30}
        db.set_setting(settings.db_path,'schedule',db.encode(schedule))
        scheduling.tick(settings)
        assert db.fetchone(settings.db_path,'SELECT COUNT(*) AS n FROM jobs')['n']==1
        waiting=current(client,identity)
        row=db.fetchone(settings.db_path,'SELECT * FROM jobs WHERE id=?',(identity,))
        activity=job_activity.snapshot(settings,row)
        assert any(event['kind']=='job_authorization_required' for event in activity['events'])
        with db.connect(settings.db_path) as conn:
            samples=[sample for family in DatabaseCollector(settings).application(conn,time.time()) for sample in family.samples]
        assert any(sample.name=='phonoteka_jobs' and sample.labels=={'kind':'audit','state':'waiting_authorization'} and sample.value==1 for sample in samples)
        assert client.get('/api/summary').json()['active_jobs']==1
        assert approve(client,owner,waiting).status_code==200
        payload=json.loads(db.fetchone(settings.db_path,'SELECT payload FROM jobs WHERE id=?',(identity,))['payload'])
        assert payload['retry_at']==retry and payload['wait_reason']=='provider'
        worker=Worker(settings)
        try:assert worker.claim_job() is None
        finally:worker.pool.shutdown()


def test_revocation_during_provider_throttle_sends_no_new_call_and_context_does_not_leak(settings):
    cfg=replace(settings,external_metadata=True)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        owner=authenticated(client,cfg);tid,_=seed(cfg)
        token,auth=issue(client,owner,['jobs:create'])
        identity=enqueue(client,auth,'analyze',[tid]).json()['id']
        worker=Worker(cfg)
        sent=[];observed=[]
        def handler(request):
            sent.append(str(request.url))
            return httpx.Response(200,json={'ok':True})
        async def exercise():
            engine=providers.Providers(cfg,transport=httpx.MockTransport(handler))
            worker.providers=engine
            engine.next['musicbrainz']=time.monotonic()+.15
            def valid_other():
                # to_thread retains the requesting task's context, including
                # while a different job is waiting in this same Providers.
                assert providers.REQUEST_AUTHORIZATION.get() is valid_other
                observed.append('other')
            async def analyze(current_job,item):
                assert providers.REQUEST_AUTHORIZATION.get() is not None
                return await engine.get('musicbrainz','https://musicbrainz.org/ws/2/recording',{'query':'synthetic'})
            worker.analyze_item=analyze
            async def unaffected_job():
                with providers.authorization(valid_other):
                    assert await engine.get('musicbrainz','https://musicbrainz.org/ws/2/recording',{'query':'other'})=={'ok':True}
            denied=asyncio.create_task(worker.process(worker.claim_job()))
            await asyncio.sleep(.03)
            await asyncio.to_thread(client.delete,'/api/tokens/'+token['id'],headers=owner)
            await unaffected_job();await denied
            waiting=current(client,identity)
            assert waiting['state']=='waiting_authorization' and waiting['authorization']['reason']=='authorization_revoked'
            assert providers.REQUEST_AUTHORIZATION.get() is None
            # The actually sent response was accounted and cached. Reading the
            # same result after another grant does not spend a second call.
            assert await engine.get('musicbrainz','https://musicbrainz.org/ws/2/recording',{'query':'other'})=={'ok':True}
            await engine.close();worker.providers=None
        try:asyncio.run(exercise())
        finally:worker.pool.shutdown()
        assert len(sent)==1 and sent[0].startswith('https://musicbrainz.org/') and observed
        calls=db.fetchall(cfg.db_path,'SELECT provider FROM provider_calls')
        assert [call['provider'] for call in calls]==['musicbrainz']
        attempts=[json.loads(row['value']) for row in db.fetchall(cfg.db_path,"SELECT value FROM settings WHERE key LIKE 'provider_attempt:%'")]
        assert len(attempts)==1 and attempts[0]['state']=='succeeded'


def test_grant_loss_after_reservation_records_not_sent_without_retry_or_budget_refund(settings):
    db.initialize(settings.db_path)
    cfg=replace(settings,external_metadata=True)
    checks=[];sent=[]
    def guard():
        checks.append(True)
        if len(checks)==2:raise media.MediaError('authorization_scope_denied','Generated authority loss before send')
    async def exercise():
        engine=providers.Providers(cfg,transport=httpx.MockTransport(lambda request:sent.append(request)))
        try:
            with providers.authorization(guard):
                with pytest.raises(media.MediaError) as error:
                    await engine.get('musicbrainz','https://musicbrainz.org/ws/2/recording',{'query':'synthetic'})
                assert error.value.code=='authorization_scope_denied'
            assert providers.REQUEST_AUTHORIZATION.get() is None
        finally:await engine.close()
    asyncio.run(exercise())
    assert len(checks)==2 and not sent
    attempts=[json.loads(row['value']) for row in db.fetchall(cfg.db_path,"SELECT value FROM settings WHERE key LIKE 'provider_attempt:%'")]
    assert len(attempts)==1 and attempts[0]['state']=='failed' and attempts[0]['code']=='not_sent'
    assert not attempts[0].get('sent_at') and attempts[0]['budget_spent']
    assert db.fetchone(cfg.db_path,'SELECT COUNT(*) AS n FROM provider_calls')['n']==1


@pytest.mark.parametrize('reason',['authorization','user_cancel'])
def test_sent_inference_is_rejoined_after_owner_grant_but_user_cancel_deletes(settings, monkeypatch, reason):
    secret=settings.state_dir/'synthetic-inference-secret';secret.parent.mkdir(exist_ok=True);secret.write_text('generated-test-only')
    cfg=replace(settings,inference_url='http://generated-inference',inference_secret_file=secret)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        owner=authenticated(client,cfg);tid,sha=seed(cfg)
        token,auth=issue(client,owner,['jobs:create'])
        identity=enqueue(client,auth,'transcribe',[tid]).json()['id']
        worker=Worker(cfg);submitted={};posts=[];deletes=[];first=[True]
        class Remote:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def get(self,path):
                document={'model':'generated','model_revision':'r1','profile_revision':'p1','contract':'transcribe-v3'} if path=='/health' else {'state':'succeeded','result':{'input_sha256':sha,'words':[]}}
                return SimpleNamespace(json=lambda:document,raise_for_status=lambda:None,status_code=200)
            async def post(self,path,json):
                remote_id=hashlib.sha256(__import__('json').dumps([json,'generated','r1','p1','transcribe-v3'],sort_keys=True).encode()).hexdigest()
                posts.append(remote_id);submitted.setdefault(remote_id,dict(json))
                if first[0]:
                    first[0]=False
                    if reason=='authorization':
                        with db.transaction(cfg.db_path) as conn:conn.execute('UPDATE api_tokens SET revoked=1 WHERE id=?',(token['id'],))
                    else:
                        with db.transaction(cfg.db_path) as conn:conn.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?',(identity,))
                return SimpleNamespace(json=lambda:{'id':remote_id},raise_for_status=lambda:None)
            async def delete(self,path):deletes.append(path)
        monkeypatch.setattr(inference_client.httpx,'AsyncClient',lambda **kwargs:Remote())
        try:
            asyncio.run(worker.process(worker.claim_job()))
            state=current(client,identity)
            if reason=='user_cancel':
                assert state['state']=='cancelled' and len(deletes)==1 and len(submitted)==1
            else:
                assert state['state']=='waiting_authorization' and not deletes
                pending=db.fetchone(cfg.db_path,'SELECT state,result FROM job_items WHERE job_id=?',(identity,))
                assert pending['state']=='deferred' and json.loads(pending['result'])['inference_job_id']==posts[0]
                assert approve(client,owner,state).status_code==200
                asyncio.run(worker.process(worker.claim_job()))
                assert current(client,identity)['state']=='succeeded'
                assert len(submitted)==len(posts)==1 and not deletes
        finally:worker.pool.shutdown()


@pytest.mark.skipif(sys.platform!='linux',reason='Native journal retirement requires Linux durable filesystem operations')
def test_prepared_native_apply_waits_and_reapproves_same_job_without_replaying_old_intent(settings,monkeypatch):
    cfg=replace(settings,read_only=False,storage_verified=True)
    path=synthetic(cfg,ext='mp3');before=media.sha256_file(path)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        owner=authenticated(client,cfg)
        enqueue_owner(client,owner,'scan');drain(cfg)
        track=client.get('/api/tracks').json()['items'][0]
        token,auth=issue(client,owner,['jobs:create','files:write'])
        proposal=client.post('/api/tracks/'+track['id']+'/proposal',json={'tags':{'title':'Explicitly renewed'}},
                             headers={**auth,'If-Match':before})
        assert proposal.status_code==201,proposal.text
        identity=enqueue(client,auth,'apply',[track['id']]).json()['id']
        original=media._transaction;revoke=[True]
        def transaction(*args,authorize=None,**kwargs):
            def at_commit():
                if revoke[0]:
                    revoke[0]=False
                    with db.transaction(cfg.db_path) as conn:conn.execute('UPDATE api_tokens SET revoked=1 WHERE id=?',(token['id'],))
                authorize()
            return original(*args,authorize=at_commit,**kwargs)
        monkeypatch.setattr(media,'_transaction',transaction)
        worker=Worker(cfg)
        try:
            asyncio.run(worker.process(worker.claim_job()))
            waiting=current(client,identity)
            assert waiting['state']=='waiting_authorization' and waiting['completed']==waiting['failed']==0
            assert media.sha256_file(path)==before
            journals=list((cfg.state_dir/'backups'/track['id']/identity).glob('*.json'))
            assert len(journals)==1
            retired=json.loads(journals[0].read_text())
            assert retired['state']=='aborted' and retired['reason']=='authorization_wait' and retired['authorization_before_verified']
            raw=journals[0].read_bytes()
            assert media.recover(journals[0],cfg.roots)['state']=='aborted'
            assert journals[0].read_bytes()==raw and media.sha256_file(path)==before
            assert approve(client,owner,waiting).status_code==200
            asyncio.run(worker.process(worker.claim_job()))
            assert current(client,identity)['state']=='succeeded'
            assert media.inspect_audio(path,cfg.roots)['tags']['title']==['Explicitly renewed']
            assert media.sha256_file(cfg.source_root/path.name)==before
            assert db.fetchone(cfg.db_path,'SELECT COUNT(*) AS n FROM history WHERE track_id=?',(track['id'],))['n']==1
            assert journals[0].read_bytes()==raw
        finally:worker.pool.shutdown()


@pytest.mark.skipif(sys.platform!='linux',reason='Native journal retirement requires Linux durable filesystem operations')
@pytest.mark.parametrize('case',['changed_target','replacing','committed','unverified_aborted'])
def test_authorization_retirement_never_skips_changed_or_ambiguous_journals(settings,case):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        authenticated(client,settings)
        tid,before=seed(settings)
        directory=settings.state_dir/'backups'/tid/'generated-job';directory.mkdir(parents=True)
        path=directory/'generated.json'
        document={'schema':1,'operation':'replace','operation_id':'generated','state':'prepared',
                  'target':str(settings.work_root/'a.mp3'),'staged':str(settings.work_root/'staged.mp3'),
                  'before_sha256':before,'after_sha256':'f'*64,'backup':str(directory/'backup.mp3')}
        if case=='changed_target':(settings.work_root/'a.mp3').write_bytes(b'concurrent input revision')
        elif case=='unverified_aborted':document.update(state='aborted',reason='authorization_wait',authorization_reason='authorization_revoked',authorization_before_verified=False)
        else:document['state']=case
        media._journal(path,document);raw=path.read_bytes()
        assert not media.retire_authorization_intent(path,settings.roots,'authorization_revoked')
        assert path.read_bytes()==raw
        if case=='unverified_aborted':
            assert media.recover(path,settings.roots)['state']=='interrupted'
