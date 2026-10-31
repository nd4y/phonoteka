"""Real SDK spans/HTTP/resolver; no music or external provider requests."""
import asyncio
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.resources import Resource

from phonoteka import tracing, processing_activity, db
from test_backend import settings, client, authenticated, seed
from test_job_activity import make_job


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({'service.name':'test'}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, '_provider', provider)
    monkeypatch.setattr(tracing, '_state', 'enabled')
    tracing.install_network()
    yield exporter
    provider.shutdown()


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.headers_seen.append(dict(self.headers))
            if self.path == '/redirect':
                self.send_response(302); self.send_header('Location','/target'); self.send_header('Content-Length','0'); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Length','2'); self.end_headers()
            time.sleep(.015); self.wfile.write(b'ok')
        def log_message(self, *args): pass
    value = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    value.headers_seen = []
    thread = threading.Thread(target=value.serve_forever,daemon=True); thread.start()
    yield value
    value.shutdown(); value.server_close(); thread.join()


def test_real_sync_http_dns_connect_body_and_privacy(spans, server):
    secret = 'secret-song-token'
    with tracing.file_attempt(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())):
        with httpx.Client(trust_env=False) as http:
            r = http.get(f'http://localhost:{server.server_port}/private/{secret}?key={secret}',
                headers={'Authorization':'Bearer '+secret,'traceparent':'00-'+'1'*32+'-'+'2'*16+'-01','baggage':secret})
            assert r.text == 'ok'
    result = spans.get_finished_spans()
    names = {s.name for s in result}
    assert {'file.attempt','http.client','dns.lookup','network.connect','http.receive_body'} <= names
    root = next(s for s in result if s.name=='file.attempt')
    http = next(s for s in result if s.name=='http.client')
    assert http.parent.span_id == root.context.span_id
    assert http.end_time-http.start_time >= 10_000_000
    assert len({s.context.trace_id for s in result}) == 1
    assert secret not in json.dumps([dict(s.attributes) for s in result])
    assert 'traceparent' not in {k.lower():v for k,v in server.headers_seen[0].items()}
    assert 'baggage' not in {k.lower():v for k,v in server.headers_seen[0].items()}


def test_async_concurrency_keeps_each_file_separate(spans, server):
    async def one():
        item = str(uuid.uuid4())
        with tracing.file_attempt(str(uuid.uuid4()),item,str(uuid.uuid4())):
            async with httpx.AsyncClient(trust_env=False) as http:
                assert (await http.get(f'http://localhost:{server.server_port}/')).status_code == 200
        return item
    async def run(): return await asyncio.gather(*(one() for _ in range(4)))
    items = asyncio.run(run())
    result = spans.get_finished_spans()
    roots = [s for s in result if s.name=='file.attempt']
    assert {s.attributes['phonoteka.item.id'] for s in roots} == set(items)
    assert len({s.context.trace_id for s in roots}) == 4
    for root in roots:
        own = [s for s in result if s.context.trace_id==root.context.trace_id]
        assert any(s.name=='dns.lookup' for s in own)
        assert any(s.name=='http.client' for s in own)
    assert not trace.get_current_span().get_span_context().is_valid


def test_aiohttp_request_and_dns_are_traced_without_private_url(spans,server):
    import aiohttp
    async def run():
        with tracing.file_attempt(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())):
            async with aiohttp.ClientSession(trust_env=False) as http:
                async with http.get(f'http://localhost:{server.server_port}/private?token=secret-aiohttp') as response:
                    assert await response.text()=='ok'
    asyncio.run(run())
    result=spans.get_finished_spans()
    request=next(s for s in result if s.name=='http.client')
    assert request.attributes['phonoteka.operation']=='response_headers'
    assert request.attributes['http.response.status_code']==200
    assert any(s.name=='dns.lookup' for s in result)
    assert 'secret-aiohttp' not in json.dumps([dict(s.attributes) for s in result])


def test_segments_bound_span_count_and_preserve_new_nested_parent(spans):
    with tracing.file_attempt(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())):
        for _ in range(127):
            with tracing.span('small'): pass
        with tracing.span('outer') as outer:
            with tracing.span('inner'): pass
    result=spans.get_finished_spans()
    traces={}
    for s in result: traces.setdefault(s.context.trace_id,[]).append(s)
    assert len(traces)==2 and all(len(group)<=128 for group in traces.values())
    continuation=next(s for s in result if s.name=='file.attempt' and s.links)
    assert continuation.attributes['phonoteka.segment.number']==2
    inner=next(s for s in result if s.name=='inner')
    assert inner.parent.span_id==outer.get_span_context().span_id
    assert inner.context.trace_id==outer.get_span_context().trace_id


def test_long_segment_rolls_on_next_operation(spans):
    with tracing.file_attempt(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())):
        tracing.SEGMENT.get()['started']-=901
        with tracing.span('next.operation'): pass
    result=spans.get_finished_spans()
    continuation=next(s for s in result if s.name=='file.attempt' and s.links)
    operation=next(s for s in result if s.name=='next.operation')
    assert operation.context.trace_id==continuation.context.trace_id
    assert operation.parent.span_id==continuation.context.span_id


def test_deadline_exports_open_intervals_without_waiting_for_business_work(spans, monkeypatch):
    monkeypatch.setattr(tracing, 'SEGMENT_SECONDS', .05)
    with tracing.file_attempt(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())):
        state = tracing.SEGMENT.get()
        with tracing.span('long.operation'):
            # Wait for the actual timer, without ending the operation/context.
            deadline = time.monotonic() + 2
            while not spans.get_finished_spans() and time.monotonic() < deadline:
                time.sleep(.005)
            first = spans.get_finished_spans()
            assert {s.name for s in first} == {'file.attempt', 'long.operation'}
            assert all(s.end_time <= state['deadline_ns'] for s in first)
            assert all(s.attributes['phonoteka.reason_code'] == 'segment_deadline' for s in first)
            with tracing.span('next.operation'): pass
        timer = state['timer']
    timer.join(timeout=1)
    assert not timer.is_alive()
    result = spans.get_finished_spans()
    assert len(result) == 4  # no duplicate ends when old contexts finally exit
    continuation = next(s for s in result if s.name == 'file.attempt' and s.links)
    assert continuation.links[0].context.trace_id == first[0].context.trace_id
    assert len({s.attributes['phonoteka.attempt.id'] for s in result if s.name == 'file.attempt'}) == 1


def test_span_budget_closes_original_root_before_attempt_finishes(spans):
    with tracing.file_attempt(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())):
        for _ in range(128):
            with tracing.span('small'): pass
        assert any(s.name == 'file.attempt' for s in spans.get_finished_spans())


def test_redirect_is_two_actual_exchanges_without_duplicate_transport_hooks(spans, server):
    with tracing.file_attempt(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())):
        with httpx.Client(trust_env=False,follow_redirects=True) as http:
            assert http.get(f'http://localhost:{server.server_port}/redirect').text=='ok'
    result=spans.get_finished_spans()
    assert len([s for s in result if s.name=='http.client'])==2
    assert len([s for s in result if s.name=='network.connect'])==2


def test_attempt_history_links_survive_new_attempt_and_stage_api(spans, settings, client):
    job = make_job(settings); item,_ = seed(settings)
    for _ in range(2):
        with processing_activity.attempt(settings.db_path,job['id'],item,str(uuid.uuid4())):
            processing_activity.action('decode')
            with processing_activity.stage('provider_http','lrclib'):
                pass
            processing_activity.outcome('succeeded')
    roots = [s for s in spans.get_finished_spans() if s.name=='file.attempt']
    assert [s.attributes['phonoteka.attempt.number'] for s in roots] == [1,2]
    assert roots[1].links[0].context.trace_id == roots[0].context.trace_id
    headers = authenticated(client,settings)
    result = client.get('/api/jobs/'+job['id']+'/activity',headers=headers).json()
    assert any(s.get('trace_id') for s in result['stage_history'])


def test_exception_messages_and_query_cannot_leak(spans):
    with pytest.raises(ValueError):
        with tracing.span('test', {'url.full':'https://x/?password=private','phonoteka.stage':'decode'}):
            raise ValueError('lyrics password private /media/song.mp3')
    value = spans.get_finished_spans()[0]
    assert value.status.status_code == trace.StatusCode.ERROR
    assert value.events == ()
    assert 'private' not in json.dumps(dict(value.attributes))


def test_export_failure_does_not_break_work_and_queue_is_bounded(spans):
    class Broken:
        def post(self,*args,**kwargs): raise OSError('credential must not be logged')
    processor = tracing.BoundedProcessor('https://invalid/v1/traces',Broken())
    with tracing.span('test'): pass
    value = spans.get_finished_spans()[0]
    # Hold consumer while filling queue to prove count bound and explicit drops.
    with processor.lock:
        for _ in range(2200): processor.on_end(value)
        assert len(processor.queue) == 2048
        assert processor.dropped == 152
        assert processor.bytes <= 8*1024*1024
    assert processor.force_flush(2000)
    assert processor.failed == 2048
    processor.shutdown()


def test_external_parent_ignored_and_diagnostics_require_auth(client,settings,spans):
    assert client.get('/api/diagnostics/tracing').status_code == 401
    headers = authenticated(client,settings)
    assert client.get('/api/diagnostics/tracing',headers=headers).json()['state']=='enabled'
    with tracing.span('http.server',parent=tracing.otcontext.Context()) as value:
        assert value.get_span_context().trace_id != int('1'*32,16)


def test_owner_enqueue_persists_own_context_and_links_attempt(client,settings,spans):
    headers=authenticated(client,settings)
    item,_=seed(settings)
    foreign='00-'+'1'*32+'-'+'2'*16+'-01'
    response=client.post('/api/jobs',json={'kind':'audit','track_ids':[item]},headers={
        **headers,'Idempotency-Key':'trace-enqueue-check','traceparent':foreign,'baggage':'secret'})
    assert response.status_code==202,response.text
    identity=response.json()['id']
    with db.connect(settings.db_path) as conn:
        payload=json.loads(conn.execute('SELECT payload FROM jobs WHERE id=?',(identity,)).fetchone()[0])
    parent=trace.get_current_span(tracing.extracted(payload['_trace'])).get_span_context()
    assert parent.is_valid and parent.trace_id!=int('1'*32,16)
    assert set(payload['_trace'])=={'traceparent'}
    with processing_activity.attempt(settings.db_path,identity,item,str(uuid.uuid4()),payload['_trace']):
        processing_activity.action('decode')
    root=next(s for s in spans.get_finished_spans() if s.name=='file.attempt')
    assert root.links[0].context.trace_id==parent.trace_id


def test_safe_grafana_link_is_optional_and_encodes_trace_query(monkeypatch):
    value='a'*32
    monkeypatch.delenv('GRAFANA_TRACE_ORIGIN',raising=False)
    assert tracing.grafana_link(value) is None
    monkeypatch.setenv('GRAFANA_TRACE_ORIGIN','https://grafana.st1.int.nd4y.icu')
    link=tracing.grafana_link(value)
    assert link.startswith('https://grafana.st1.int.nd4y.icu/explore?')
    assert 'phonoteka-jaeger' in link and value in link
    assert tracing.grafana_link('https://attacker/') is None


def test_internal_rpc_uses_client_span_parent_external_rpc_cannot_receive_it(spans,server,monkeypatch):
    monkeypatch.setenv('INFERENCE_URL',f'http://localhost:{server.server_port}')
    with tracing.file_attempt(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())):
        with httpx.Client(trust_env=False) as http:
            http.get(f'http://localhost:{server.server_port}/')
    client_span=next(s for s in spans.get_finished_spans() if s.name=='http.client')
    headers={k.lower():v for k,v in server.headers_seen[0].items()}
    parent=trace.get_current_span(tracing.extracted(headers)).get_span_context()
    assert parent.trace_id==client_span.context.trace_id and parent.span_id==client_span.context.span_id
