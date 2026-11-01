"""Queue control regressions use real owner routes, never forged job states."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import threading
import time
import wave
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from phonoteka import db, media
from phonoteka.app import create_app
from phonoteka.worker import Worker
from test_backend import settings, authenticated, job
import phonoteka.app as app_module


def native_catalog(settings, count):
    """Physical tiny WAV files; only catalog fixture setup is batched in SQL."""
    stream=io.BytesIO()
    with wave.open(stream,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000)
        audio.writeframes(bytes(800))
    content=stream.getvalue()
    first=settings.work_root/'00000.wav';first.write_bytes(content)
    metadata=media.inspect_audio(first,settings.roots)
    metadata['lrc']=metadata.get('lrc') or '';metadata['lyrics']=metadata.get('lyrics') or ''
    ids=[f'admission-{index:05}' for index in range(count)]
    def rows():
        for index,identity in enumerate(ids):
            path=settings.work_root/f'{index:05}.wav'
            if index:path.write_bytes(content)
            now=db.utcnow()
            yield(identity,path.name,metadata['sha256'],db.encode({**metadata,'mtime_ns':path.stat().st_mtime_ns}),now,now)
    with db.transaction(settings.db_path) as conn:
        conn.executemany('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',rows())
    return ids,metadata['sha256']


def test_once_repolls_when_lane_finishes_during_busy_claim(settings,monkeypatch):
    cfg=replace(settings,read_only=False,storage_verified=True,file_workers=1)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);ids,_=native_catalog(cfg,1)
        one=job(client,headers,'audit',ids);two=job(client,headers,'audit',ids)
        worker=Worker(cfg);release=threading.Event();forced=[];real_claim=worker.claim_job
        def operation(current,item):
            assert release.wait(10),'finite audit barrier was not released'
            return {'state':'passed'}
        worker.process_item=operation
        async def exercise():
            loop=asyncio.get_running_loop()
            async def joined(task):await asyncio.shield(task)
            def claim():
                row=real_claim()
                if row is None and not forced and worker.normal_task and not worker.normal_task.done():
                    forced.append(True);release.set()
                    asyncio.run_coroutine_threadsafe(joined(worker.normal_task),loop).result(10)
                    # This empty result was computed with the old busy lane.
                    assert worker.normal_task.done()
                return row
            monkeypatch.setattr(worker,'claim_job',claim)
            try:await worker.run(once=True)
            finally:release.set()
        asyncio.run(exercise())
        assert forced==[True]
        assert client.get('/api/jobs/'+one['id']).json()['state']=='succeeded'
        assert client.get('/api/jobs/'+two['id']).json()['state']=='succeeded'


def test_same_timestamp_jobs_follow_submission_order_not_random_uuid(settings,monkeypatch):
    cfg=replace(settings,read_only=False,storage_verified=True)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);ids,_=native_catalog(cfg,1)
        fixed=db.utcnow();monkeypatch.setattr(db,'utcnow',lambda:fixed)
        identities=iter([uuid.UUID('ffffffff-ffff-4fff-8fff-ffffffffffff'),uuid.UUID('00000000-0000-4000-8000-000000000001')])
        monkeypatch.setattr(app_module,'uuid',SimpleNamespace(uuid4=lambda:next(identities),UUID=uuid.UUID))
        first=job(client,headers,'audit',ids);second=job(client,headers,'audit',ids)
        assert first['id']>second['id'] and first['created_at']==second['created_at']
        worker=Worker(cfg)
        try:assert worker.claim_job()['id']==first['id']
        finally:worker.pool.shutdown()


def controlled_segment(settings, client, headers, job_id, calls, control):
    """Keep every file slot in flight while a real HTTP control is acknowledged."""
    worker=Worker(settings)
    all_started=threading.Event();release=threading.Event();requested=threading.Event()
    mutex=threading.Lock();started=[];finished=[];pulled=[]
    bound=settings.file_workers*3+1  # active + queue + one blocked producer put
    original=worker.bounded_items

    def operation(current,item):
        with mutex:
            started.append(item['track_id']);calls[item['track_id']]+=1
            if len(started)==settings.file_workers:all_started.set()
        assert release.wait(10),'in-flight file barrier exceeded its bound'
        with mutex:finished.append(item['track_id'])
        return {'state':'passed'}

    async def observed(items,fn,**kwargs):
        def source():
            for item in items:
                pulled.append(item['track_id'])
                # Fail quickly on the original bug rather than spend a minute
                # decoding the giant payload for all10,000 skipped items.
                if requested.is_set() and len(pulled)>bound:
                    raise AssertionError('producer enumerated the stopped remainder')
                yield item
        return await original(source(),fn,**kwargs)

    worker.process_item=operation;worker.bounded_items=observed
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(lambda:asyncio.run(worker.run(once=True)))
        try:
            assert all_started.wait(10),'all file workers did not start'
            before=client.get('/api/jobs/'+job_id).json()
            assert before['state']=='running' and before['active_count']==settings.file_workers
            control()
            requested.set()
            assert not finished,'the barrier must still hold already started work'
        finally:release.set()
        future.result(timeout=10)
    state=client.get('/api/jobs/'+job_id).json()
    assert len(pulled)<=bound,'admission must stop after only bounded pending items'
    assert len(started)==len(finished)==settings.file_workers
    assert set(started)==set(finished),'every already started operation must finish'
    assert state['active_count']==state['failed']==0
    persisted=db.fetchall(settings.db_path,'SELECT item_key,state,result FROM job_items WHERE job_id=?',(job_id,))
    assert len(persisted)==state['completed']
    assert all(row['state']=='succeeded' for row in persisted)
    assert all(json.loads(row['result'])['state']=='passed' for row in persisted)
    # The API flattens the operation result after the durable lifecycle state;
    # the synthetic operation's "passed" outcome is therefore public here.
    assert len(state['results'])==state['completed']
    assert all(row['state']=='passed' for row in state['results'])
    return state,set(started)


@pytest.mark.parametrize('action',['cancel','pause'])
def test_ten_thousand_item_job_stops_admission_and_drains_then_resumes(settings, action):
    cfg=replace(settings,file_workers=4,io_threads=4)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);ids,original_sha=native_catalog(cfg,10000)
        queued=job(client,headers,'audit',ids);calls=Counter()
        def request_control():
            response=client.post('/api/jobs/'+queued['id']+'/'+action,headers=headers)
            assert response.status_code==200,response.text
        state,first=controlled_segment(cfg,client,headers,queued['id'],calls,request_control)
        assert state['total']==10000 and state['completed']==4
        assert state['state']==('cancelled_partial' if action=='cancel' else 'paused')
        if action=='pause':
            response=client.post('/api/jobs/'+queued['id']+'/resume',headers=headers)
            assert response.status_code==200 and response.json()['completed']==4
            def pause_again():
                response=client.post('/api/jobs/'+queued['id']+'/pause',headers=headers)
                assert response.status_code==200,response.text
            resumed,second=controlled_segment(cfg,client,headers,queued['id'],calls,pause_again)
            assert resumed['state']=='paused' and resumed['completed']==8 and resumed['failed']==0
            assert first.isdisjoint(second) and set(calls.values())=={1}
            cancelled=client.post('/api/jobs/'+queued['id']+'/cancel',headers=headers).json()
            assert cancelled['state']=='cancelled_partial' and cancelled['completed']==8
        assert media.sha256_file(cfg.work_root/'00000.wav')==original_sha
        assert media.sha256_file(cfg.work_root/'09999.wav')==original_sha


def test_live_token_revoke_drains_started_work_and_waits_for_explicit_owner_grant(settings):
    cfg=replace(settings,file_workers=4,io_threads=4)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);ids,_=native_catalog(cfg,512)
        issued=client.post('/api/tokens',json={'name':'queue principal','scopes':['read','jobs:analysis']},headers=headers)
        assert issued.status_code==201
        token=issued.json();token_headers={'Authorization':'Bearer '+token['token'],'Origin':cfg.app_url}
        queued=job(client,token_headers,'audit',ids)
        def revoke():
            response=client.delete('/api/tokens/'+token['id'],headers=headers)
            assert response.status_code==200,response.text
        state,_=controlled_segment(cfg,client,headers,queued['id'],Counter(),revoke)
        assert state['state']=='waiting_authorization' and state['completed']==4
        assert not state['cancel_requested'] and state['authorization']['reauthorize_allowed']
        assert client.get('/api/tracks',headers=token_headers).status_code==401
        assert client.post('/api/jobs/'+queued['id']+'/resume',headers=headers).status_code==409


def test_provider_wait_stops_large_queue_admission_without_failed_or_completed_items(settings):
    cfg=replace(settings,file_workers=1,io_threads=1,external_fingerprints=True)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);ids,_=native_catalog(cfg,512)
        queued=job(client,headers,'recognize',ids);worker=Worker(cfg)
        calls=[];pulled=[];waiting=False;original=worker.bounded_items
        async def analyze(current,item):
            nonlocal waiting
            calls.append(item['track_id']);waiting=True
            return {'state':'incomplete','retry_at':time.time()+600}
        async def observed(items,fn,**kwargs):
            def source():
                for item in items:
                    pulled.append(item['track_id'])
                    if waiting and len(pulled)>4:raise AssertionError('retry enumerated the pending remainder')
                    yield item
            return await original(source(),fn,**kwargs)
        worker.analyze_item=analyze;worker.bounded_items=observed
        try:asyncio.run(worker.process(worker.claim_job()))
        finally:worker.pool.shutdown()
        state=client.get('/api/jobs/'+queued['id']).json()
        assert len(calls)==1 and len(pulled)<=4
        assert state['state']=='queued' and state['stage']=='waiting_provider'
        assert state['total']==512 and state['completed']==state['failed']==0
        assert state['retry_at']>time.time() and state['eta_reason']=='waiting_provider'
        persisted=db.fetchall(cfg.db_path,'SELECT item_key,state,result FROM job_items WHERE job_id=?',(queued['id'],))
        assert len(persisted)==1 and all(row['state']=='deferred' for row in persisted)
        assert persisted[0]['item_key']==calls[0]
        outcome=json.loads(persisted[0]['result'])
        assert outcome['state']=='incomplete' and outcome['retry_at']==state['retry_at']
        assert len(state['results'])==1 and state['results'][0]['state']=='incomplete'
        assert state['results'][0]['retry_at']==state['retry_at']


def test_consumer_error_with_full_ten_thousand_source_does_not_deadlock(settings):
    async def exercise():
        worker=Worker(settings);full=asyncio.Event();release=asyncio.Event();pulled=[];calls=[]
        bound=settings.file_workers*3+1
        def source():
            for index in range(10000):
                pulled.append(index)
                if len(pulled)==bound:full.set()
                yield index
        async def operation(item):
            calls.append(item)
            await release.wait()
            if item==0:raise RuntimeError('consumer failed with producer blocked')
            await asyncio.Event().wait()
        task=asyncio.create_task(worker.bounded_items(source(),operation))
        try:
            await asyncio.wait_for(full.wait(),timeout=2)
            release.set()
            with pytest.raises(ExceptionGroup) as failure:
                await asyncio.wait_for(task,timeout=2)
            assert 'consumer failed' in str(failure.value.exceptions[0])
            assert len(pulled)<=bound and len(calls)==settings.file_workers
        finally:
            if not task.done():
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
            worker.pool.shutdown()
    asyncio.run(exercise())
