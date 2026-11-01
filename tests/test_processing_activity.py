import asyncio
import json
import time
import uuid

import httpx
import pytest

from phonoteka import db, job_activity, processing_activity as activity
from phonoteka.providers import Providers, ProviderError
from phonoteka.worker import Worker
from test_backend import settings, client, authenticated, seed
from test_job_activity import make_job
from test_providers import settings as provider_settings


def identity(): return str(uuid.uuid4())


def test_stage_measurements_are_durable_and_owner_only(client, settings):
    row = make_job(settings)
    track, _ = seed(settings)
    generation = identity()
    with activity.attempt(settings.db_path, row['id'], track, generation):
        activity.action('decode')
        with activity.stage('provider_http', 'lrclib') as measured:
            measured['http_status'] = 200
        activity.outcome('succeeded')
    events = job_activity.event_rows(db.connect(settings.db_path), [row['id']])
    projection = activity.project(events, [], {'at': time.time(), 'activity_generation': generation}, time.time())
    http = next(value for value in projection['stage_history'] if value['stage']=='provider_http')
    assert http['provider']=='lrclib' and http['duration_ms'] is not None and http['http_status']==200
    assert http['state']=='succeeded' and http['progress_observability']=='unavailable'
    assert client.get('/api/jobs/'+row['id']+'/activity').status_code == 401
    headers = authenticated(client, settings)
    result = client.get('/api/jobs/'+row['id']+'/activity', headers=headers).json()
    assert any(value['filename']=='a.mp3' for value in result['stage_history'])


def test_unknown_exception_text_and_untrusted_provider_never_exported(client, settings):
    row = make_job(settings)
    with activity.attempt(settings.db_path, row['id'], identity(), identity()):
        with pytest.raises(ValueError):
            with activity.stage('provider_http', 'secret /media/song.mp3'):
                raise ValueError('secret text token=12345')
    events = db.fetchall(settings.db_path, "SELECT payload FROM events WHERE kind='job_stage'")
    raw = json.dumps([dict(value) for value in events])
    assert 'secret' not in raw and 'token=12345' not in raw and '/media' not in raw


def test_telemetry_write_failure_does_not_fail_work(client, settings, monkeypatch):
    def broken(*args, **kwargs): raise OSError('private path')
    monkeypatch.setattr(db, 'event', broken)
    with activity.attempt(settings.db_path, identity(), identity(), identity()) as current:
        activity.action('decode')
        with activity.stage('provider_http', 'lrclib'): pass
        activity.outcome('succeeded')
    assert current.partial and current.outcome=='succeeded' and activity.CURRENT.get() is None


def test_parallel_thread_offload_keeps_each_attempt_context(settings, monkeypatch):
    seen=[]
    monkeypatch.setattr(activity.Attempt, 'emit', lambda self, value: seen.append((self.item_id, value['stage'])))
    worker = Worker(settings)
    async def one(track):
        with activity.attempt(settings.db_path, identity(), track, identity()):
            await worker.io(activity.action, 'decode')
            await asyncio.sleep(0)
            assert activity.CURRENT.get().item_id==track
            activity.outcome('succeeded')
    async def run(): await asyncio.gather(one('first'), one('second'))
    try: asyncio.run(run())
    finally: worker.pool.shutdown()
    assert ('first','decode') in seen and ('second','decode') in seen
    assert activity.CURRENT.get() is None


def test_old_generation_cannot_show_running_and_unknown_duration(client, settings):
    row = make_job(settings)
    track = identity()
    with activity.attempt(settings.db_path, row['id'], track, identity()): pass
    events=job_activity.event_rows(db.connect(settings.db_path), [row['id']])
    # Model a process crash: only its initial running transition reached disk.
    events=[event for event in events if event['data']['state']=='running']
    result=activity.project(events, [{'item_key':track,'state':'running'}],
                            {'at':time.time(),'activity_generation':identity()}, time.time())
    assert not result['current_stages']
    assert result['stage_history'][0]['state']=='interrupted'
    assert result['stage_history'][0]['duration_ms'] is None


def test_live_heartbeat_is_not_useful_progress(client, settings, monkeypatch):
    row = make_job(settings)
    track, generation=identity(), identity()
    with activity.attempt(settings.db_path, row['id'], track, generation):
        activity.action('write_tags')
        events=job_activity.event_rows(db.connect(settings.db_path), [row['id']])
        now=time.time()+70
        result=activity.project(events,[{'item_key':track,'state':'running'}],{'at':now,'activity_generation':generation},now)
        assert result['current_stages'][0]['long_running']
        assert result['current_stages'][0]['activity_health']=='responding'
        assert result['progress_observability']=='unavailable'
        stale=activity.project(events,[{'item_key':track,'state':'running'}],{'at':now-61,'activity_generation':generation},now)
        assert stale['executor_health']=='executor_unavailable'


def test_real_provider_429_carries_http_and_reason_without_query(tmp_path):
    cfg=provider_settings(tmp_path)
    async def run():
        provider=Providers(cfg,httpx.MockTransport(lambda request:httpx.Response(429, headers={'retry-after':'60'})))
        with activity.attempt(cfg.db_path,identity(),identity(),identity()):
            with pytest.raises(ProviderError) as error:
                await provider.get('lrclib','https://lrclib.net/api/search',{'q':'PRIVATE SONG'})
            assert error.value.code=='rate_limited'
        await provider.close()
    asyncio.run(run())
    events=[json.loads(row['payload']) for row in db.fetchall(cfg.db_path,"SELECT payload FROM events WHERE kind='job_stage'")]
    http=[value for value in events if value['stage']=='provider_http' and value['state']=='failed']
    assert len(http)==1 and http[0]['http_status']==429 and http[0]['reason_code']=='rate_limited'
    assert 'PRIVATE SONG' not in json.dumps(events)


def test_waits_keep_multiple_providers_and_unknown_eta(client,settings):
    row=make_job(settings,state='queued',stage='waiting_provider')
    track,_=seed(settings)
    result={'retry_at':time.time()+60,'sources':[{'provider':'lrclib','state':'provider_unavailable'}, {'provider':'musicbrainz','state':'budget_exhausted'}]}
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO job_items VALUES(?,?,?,?)',(row['id'],track,'deferred',db.encode(result)))
    result=job_activity.snapshot(settings,row)
    assert {wait['provider'] for wait in result['provider_waits']}=={'lrclib','musicbrainz'}
    assert result['summary']['eta_seconds'] is None and not any(wait['overdue_wait'] for wait in result['provider_waits'])


def test_only_real_advancing_checkpoints_create_progress(client, settings):
    row=make_job(settings)
    track, generation=identity(), identity()
    with activity.attempt(settings.db_path,row['id'],track,generation):
        with activity.stage('provider_http','lrclib'):
            activity.checkpoint(10,'bytes')
            first=db.fetchone(settings.db_path,"SELECT COUNT(*) n FROM events WHERE kind='job_stage'")['n']
            activity.checkpoint(10,'bytes')
            assert db.fetchone(settings.db_path,"SELECT COUNT(*) n FROM events WHERE kind='job_stage'")['n']==first
            events=job_activity.event_rows(db.connect(settings.db_path),[row['id']])
            now=job_activity._timestamp(events[-1]['data']['last_progress_at'])+121
            result=activity.project(events,[{'item_key':track,'state':'running'}],{'at':now,'activity_generation':generation},now)
            assert result['current_stages'][0]['no_progress_suspected']
            assert result['current_stages'][0]['progress_counter']==10
            assert result['progress_observability']=='partial'


def test_malformed_or_pruned_measurements_never_invent_success(client,settings):
    row=make_job(settings)
    with db.transaction(settings.db_path) as conn:
        db.event(conn,'job_stage',{'job_id':row['id'],'stage':['bad'],'provider':{'secret':'x'}})
    result=job_activity.snapshot(settings,row)
    assert not result['stage_history']
    assert 'secret' not in json.dumps(result)


def test_late_old_generation_end_does_not_hide_current_attempt(client,settings):
    row=make_job(settings)
    track, old_generation, generation=identity(), identity(), identity()
    with activity.attempt(settings.db_path,row['id'],track,old_generation): pass
    old=job_activity.event_rows(db.connect(settings.db_path),[row['id']])[-1]
    with activity.attempt(settings.db_path,row['id'],track,generation):
        activity.action('decode')
        events=job_activity.event_rows(db.connect(settings.db_path),[row['id']])
        events.append({**old,'id':events[-1]['id']+1})
        result=activity.project(events,[{'item_key':track,'state':'running'}],{'at':time.time(),'activity_generation':generation},time.time())
        assert len(result['current_stages'])==1
        assert result['current_stages'][0]['executor_instance_id']==generation
