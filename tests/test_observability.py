import asyncio
from dataclasses import replace
import io
import json
import logging
import urllib.error
import urllib.request
import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from phonoteka import db
from phonoteka import media,provider_policy
from phonoteka.app import create_app
from phonoteka.metrics import DatabaseCollector
from phonoteka.providers import Providers, ProviderError
from phonoteka.telemetry import Metrics, JsonFormatter, context, worker_server
from phonoteka.telemetry import policy_denied
from phonoteka.worker import Worker
from test_backend import settings, authenticated, seed, job

TOKEN='test-metrics-token-distinct-from-owner-password'


def configured(settings):
    secret=settings.state_dir/'scrape-token';secret.write_text(TOKEN)
    return replace(settings,metrics_token_file=secret)


def samples(body):
    return { (s.name,tuple(sorted(s.labels.items()))):s.value
             for family in text_string_to_metric_families(body) for s in family.samples }


def test_metrics_require_dedicated_token_even_for_owner(settings):
    cfg=configured(settings)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        assert client.get('/metrics').status_code==401
        authenticated(client,cfg)
        assert client.get('/metrics').status_code==401
        assert client.get('/metrics',headers={'Authorization':'Bearer wrong'}).status_code==401
        seed(cfg,'private-title.mp3');response=client.get('/metrics',headers={'Authorization':'Bearer '+TOKEN})
        assert response.status_code==200 and 'text/plain' in response.headers['content-type']
        values=samples(response.text)
        assert values['phonoteka_collector_up',()]==1
        assert values['phonoteka_collection_files',()]==1
        assert values['phonoteka_worker_online',()]==0
        assert all(secret not in response.text for secret in ['private-title','Example','Artist',TOKEN,str(cfg.work_root)])


def test_http_route_labels_and_request_id_do_not_accept_private_values(settings,caplog):
    cfg=configured(settings)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client,caplog.at_level(logging.INFO,logger='phonoteka'):
        response=client.get('/api/tracks/private-secret-path?search=private-title',headers={'X-Request-ID':'secret-token'})
        assert response.status_code==401
        assert len(response.headers['x-request-id'])==32 and response.headers['x-request-id']!='secret-token'
        client.get('/unmatched-private-path?access_token=secret-token')
        body=client.get('/metrics',headers={'Authorization':'Bearer '+TOKEN}).text
        assert 'route="/api/tracks/{track_id}"' in body and 'route="unmatched"' in body
        assert 'private-' not in body and 'secret-token' not in body
    formatted='\n'.join(JsonFormatter('app').format(r) for r in caplog.records if r.name=='phonoteka')
    assert 'private-' not in formatted and 'secret-token' not in formatted
    assert 'http_request' in formatted


def test_json_formatter_omits_library_messages_arguments_and_exception_text():
    record=logging.LogRecord('httpx',logging.ERROR,__file__,1,'GET https://x/?token=%s',('very-private-token',),None)
    try: raise ValueError('private-password and private-song.mp3')
    except ValueError:
        import sys
        record.exc_info=sys.exc_info()
    with context(request_id='safe-generated-id'):
        data=json.loads(JsonFormatter('worker').format(record))
    assert data['error_type']=='ValueError' and data['stack'] and data['request_id']=='safe-generated-id'
    assert 'private' not in json.dumps(data)
    assert data['timestamp'].endswith('+00:00') and data['service']=='worker'


def test_missing_db_is_reported_without_creating_a_database(settings):
    collector=DatabaseCollector(settings)
    registry=Metrics('app').registry;registry.register(collector)
    from prometheus_client import generate_latest
    assert samples(generate_latest(registry).decode())['phonoteka_collector_up',()]==0
    assert not settings.db_path.exists()


def test_queue_and_provider_snapshots_are_gauges_and_survive_new_registry(settings):
    cfg=configured(settings)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);track,_=seed(cfg)
        queued=job(client,headers,'youtube',[track])
        with db.transaction(cfg.db_path) as conn:
            conn.execute("INSERT INTO provider_calls(provider,at) VALUES('youtube',?)",(__import__('time').time(),))
        registry=Metrics('app').registry;registry.register(DatabaseCollector(cfg))
        from prometheus_client import generate_latest
        values=samples(generate_latest(registry).decode())
        assert values['phonoteka_jobs',(('kind','youtube'),('state','queued'))]==1
        assert values['phonoteka_queue_items_remaining',(('kind','youtube'),)]==1
        assert values['phonoteka_provider_requests_24h',(('provider','youtube'),)]==1
        worker=Worker(cfg);asyncio.run(worker.run(once=True))
        values=samples(worker.metrics.render().decode())
        assert values['phonoteka_item_attempts_total',(('kind','youtube'),('state','succeeded'))]==1
        assert values['phonoteka_jobs_in_progress',(('kind','youtube'),)]==0
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'


def test_provider_success_cache_and_rate_limit_are_instrumented(settings):
    db.initialize(settings.db_path)
    db.set_setting(settings.db_path,'provider_usage:lrclib:lyrics',db.encode({'allow_cache':True,'cache_ttl':3600,'basis':'Explicit cache permission for metrics fixture'}))
    async def run():
        async with httpx.AsyncClient():
            provider=Providers(settings,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'safe':True})))
            assert await provider.get('lrclib','https://lrclib.net/api/get',{'track':'private-title'})=={'safe':True}
            await provider.get('lrclib','https://lrclib.net/api/get',{'track':'private-title'})
            values=samples(provider.metrics.render().decode())
            assert values['phonoteka_provider_operations_total',(('operation','metadata'),('outcome','success'),('provider','lrclib'))]==2
            assert values['phonoteka_provider_cache_hits_total',(('provider','lrclib'),)]==1
            assert 'private-title' not in provider.metrics.render().decode()
            await provider.close()
            provider=Providers(settings,transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'60'})))
            with pytest.raises(ProviderError):await provider.get('youtube','https://www.youtube.com/oembed')
            values=samples(provider.metrics.render().decode())
            assert values['phonoteka_provider_operations_total',(('operation','metadata'),('outcome','rate_limited'),('provider','youtube'))]==1
            await provider.close()
    asyncio.run(run())


def test_worker_metrics_listener_is_private_and_authenticated(settings):
    cfg=configured(settings);metrics=Metrics('worker');server=worker_server(metrics,cfg.metrics_token_file,0)
    try:
        url='http://127.0.0.1:'+str(server.server_port)+'/metrics'
        with pytest.raises(urllib.error.HTTPError) as exc:urllib.request.urlopen(url)
        assert exc.value.code==401
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+TOKEN})
        with urllib.request.urlopen(request) as response: assert b'phonoteka_build_info' in response.read()
    finally:server.shutdown();server.server_close()


def test_component_proxy_only_uses_fixed_internal_targets(settings,monkeypatch):
    cfg=configured(settings);real=httpx.AsyncClient;seen=[]
    def transport(request):
        seen.append(request);return httpx.Response(200,text='# TYPE fixture gauge\nfixture 1\n')
    monkeypatch.setattr('phonoteka.observability.httpx.AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(transport),**kwargs))
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        auth={'Authorization':'Bearer '+TOKEN}
        assert client.get('/metrics/worker').status_code==401
        assert client.get('/metrics/unknown',headers=auth).status_code==404
        response=client.get('/metrics/worker',headers=auth);assert response.status_code==200
    assert str(seen[0].url)=='http://worker:9000/metrics' and seen[0].headers['authorization']=='Bearer '+TOKEN


def test_inference_metrics_token_is_separate_from_asr_key(settings,monkeypatch):
    from phonoteka import inference
    cfg=configured(settings);key=settings.state_dir/'asr-key';key.write_text('different-asr-key')
    monkeypatch.setattr(inference,'STATE',settings.state_dir);monkeypatch.setattr(inference,'SECRET',key);monkeypatch.setattr(inference,'METRICS_SECRET',cfg.metrics_token_file)
    with TestClient(inference.app) as client:
        assert client.get('/metrics',headers={'Authorization':'Bearer different-asr-key'}).status_code==401
        assert client.get('/metrics',headers={'Authorization':'Bearer '+TOKEN}).status_code==200
        assert client.get('/jobs/not-a-job',headers={'Authorization':'Bearer '+TOKEN}).status_code==401


def test_storage_wait_and_webhook_gauges_have_only_bounded_labels(settings):
    cfg=configured(settings)
    with TestClient(create_app(cfg),base_url=cfg.app_url):
        now=__import__('time').time()
        with db.transaction(cfg.db_path) as conn:
            for identity,kind,stage,delay in [('private-storage-id','apply','waiting_storage',60),('private-provider-id','recognize','waiting_provider',120)]:
                conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,completed,created_at,updated_at) VALUES(?,?,'queued',?,?,4,1,?,?)",(identity,kind,stage,db.encode({'retry_at':now+delay,'private_path':'/private/music'}),db.utcnow(),db.utcnow()))
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at) VALUES('cancelled-stale-wait','apply','cancelled','waiting_storage',?,9,?,?)",(db.encode({'retry_at':now+999}),db.utcnow(),db.utcnow()))
            for index,state in enumerate(('pending','in_flight','delivered','dead')):
                conn.execute('INSERT INTO webhook_outbox(id,event_id,destination_id,job_id,payload,state,next_attempt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (f'private-event-{index}',index+1,'private-destination',f'private-job-{index}',db.encode({'token':'private-signing-value'}),state,now+45,db.utcnow(),db.utcnow()))
            conn.execute("INSERT INTO webhook_reservations VALUES('private-reserved-job','private-destination')")
            conn.execute("UPDATE settings SET value=? WHERE key='webhook_runtime'",(db.encode({'enabled':False,'max_pending':42,'destination_id':'private-destination'}),))
        registry=Metrics('app').registry;registry.register(DatabaseCollector(cfg))
        from prometheus_client import generate_latest
        body=generate_latest(registry).decode();values=samples(body)
        assert values['phonoteka_storage_wait_jobs',(('kind','apply'),)]==1
        assert values['phonoteka_storage_wait_items_remaining',(('kind','apply'),)]==3
        assert 0<values['phonoteka_storage_retry_wait_seconds',(('kind','apply'),)]<=60
        assert values['phonoteka_provider_retry_jobs',(('kind','recognize'),)]==1
        assert values['phonoteka_webhook_outbox',(('state','dead'),)]==1
        assert values['phonoteka_webhook_reserved_jobs',()]==1
        assert values['phonoteka_webhook_queue_slots_used',()]==3
        assert values['phonoteka_webhook_queue_capacity',()]==42
        assert values['phonoteka_webhook_delivery_enabled',()]==0
        assert values['phonoteka_webhook_other_destination_pending',()]==0
        assert all(private not in body for private in ('private-event','private-destination','private-job','private-signing-value','private-storage-id','/private/music',TOKEN))


def test_provider_policy_denials_counter_does_not_use_dynamic_or_private_labels(settings):
    db.initialize(settings.db_path)
    metrics=Metrics('app')
    label=('phonoteka_provider_policy_denials_total',(('operation','embed'),('provider','lrclib')))
    before=samples(metrics.render().decode())[label]
    with db.connect(settings.db_path) as conn,pytest.raises(media.MediaError):
        provider_policy.require_sources(conn,[{'origin':'provider_acquired','provider':'lrclib','data_type':'lyrics'}],'embed')
    assert samples(metrics.render().decode())[label]==before+1
    policy_denied('private-provider-input','private-operation-input')
    body=metrics.render().decode();values=samples(body)
    assert values['phonoteka_provider_policy_denials_total',(('operation','other'),('provider','other'))]>=1
    assert 'private-provider-input' not in body and 'private-operation-input' not in body


def test_item_action_stdout_uses_only_public_phase_codes():
    formatter = JsonFormatter('worker')
    record = logging.LogRecord('phonoteka', logging.INFO, '', 0, 'ignored', (), None)
    record.event = 'item_action'
    record.fields = {'action':'metadata_lookup'}
    assert json.loads(formatter.format(record))['action'] == 'metadata_lookup'
    record.fields = {'action':'/private/music/secret-title.flac'}
    rendered = formatter.format(record)
    assert json.loads(rendered)['action'] == 'other' and 'secret-title' not in rendered
