"""Opt-in, bounded OTel tracing. Never exports paths, query strings or bodies.

No automatic instrumentation: its default URL/exception capture is inappropriate
for music/provider credentials. Only the allowlisted attributes below cross OTLP.
"""
import asyncio
import atexit
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import re
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

from opentelemetry import trace, context as otcontext
from opentelemetry.trace import SpanKind, Status, StatusCode, Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, SpanLimits, SpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

ALLOWED = {'phonoteka.job.id', 'phonoteka.item.id', 'phonoteka.attempt.id',
    'phonoteka.attempt.number', 'phonoteka.operation', 'phonoteka.stage',
    'phonoteka.provider', 'phonoteka.outcome', 'phonoteka.reason_code',
    'phonoteka.cache.hit', 'http.request.method', 'http.response.status_code',
    'http.route', 'server.address', 'server.port', 'url.scheme', 'error.type',
    'dns.question.name', 'dns.question.type', 'dns.answers.count',
    'network.transport', 'network.protocol.name', 'phonoteka.segment.number'}
IDS = {'phonoteka.job.id', 'phonoteka.item.id', 'phonoteka.attempt.id'}
HOST = re.compile(r'^[a-zA-Z0-9_.:-]{1,253}$')
OPAQUE = re.compile(r'^(?:[a-f0-9]{32,64}|[a-f0-9-]{36})$')
PROPAGATOR = TraceContextTextMapPropagator()
SEGMENT = ContextVar('trace_segment', default=None)
_provider = None
_processor = None
_patched = False
_state = 'disabled'
SEGMENT_SECONDS = 900


class SegmentSpan(trace.Span):
    """Idempotent SDK span facade shared by application and deadline thread."""
    def __init__(self, inner, state):
        self.inner, self.state, self.closed = inner, state, False

    def get_span_context(self): return self.inner.get_span_context()
    def is_recording(self): return not self.closed and self.inner.is_recording()
    def set_attribute(self, key, value):
        with self.state['lock']:
            if not self.closed: self.inner.set_attribute(key, value)
    def set_attributes(self, attributes):
        with self.state['lock']:
            if not self.closed: self.inner.set_attributes(attributes)
    def add_event(self, name, attributes=None, timestamp=None):
        with self.state['lock']:
            if not self.closed: self.inner.add_event(name, attributes, timestamp)
    def set_status(self, status, description=None):
        with self.state['lock']:
            if not self.closed: self.inner.set_status(status, description)
    def update_name(self, name):
        with self.state['lock']:
            if not self.closed: self.inner.update_name(name)
    def record_exception(self, exception, attributes=None, timestamp=None, escaped=False):
        # Exception messages/stacks can contain paths, lyrics and credentials.
        self.set_attribute('error.type', type(exception).__name__)
    def end(self, end_time=None):
        with self.state['lock']:
            if self.closed: return
            self.closed = True
            self.inner.end(end_time=end_time)
            self.state['active'].remove(self)


def expire_segment(state, number):
    """Export bounded partial intervals without cancelling business work."""
    with state['lock']:
        if state['finished'] or state['number'] != number: return
        for current in list(reversed(state['active'])):
            current.set_attribute('phonoteka.reason_code', 'segment_deadline')
            current.end(end_time=state['deadline_ns'])
        state['expired'] = True


def safe(attributes):
    result = {}
    for key, value in attributes.items():
        if key not in ALLOWED or value is None: continue
        if key in IDS and not (isinstance(value, str) and OPAQUE.fullmatch(value)): continue
        if isinstance(value, str): value = value.encode('utf-8')[:256].decode('utf-8', 'ignore')
        if type(value) in (str, bool, int, float): result[key] = value
    return result


def attributes(**values):
    return {'phonoteka.' + key.replace('_', '.'): value for key, value in values.items()}


class BoundedProcessor(SpanProcessor):
    """Queue is capped by count AND bytes; network work is off the event loop.

    The SDK exporter retries internally beyond our deadline, so we encode with
    its pinned protobuf encoder but use one bounded HTTP attempt per batch.
    Collector owns persistent retries. Loss is explicit and never business retry.
    """
    def __init__(self, endpoint, session):
        self.endpoint, self.session = endpoint, session
        self.queue = deque(); self.bytes = 0
        self.lock = threading.Condition(); self.sending = False; self.stopping = False
        self.accepted = self.exported = self.dropped = self.failed = self.partial = 0
        self.last_failure = None
        self.thread = threading.Thread(target=self._run, daemon=True, name='otlp-export')
        self.thread.start()

    def on_start(self, span, parent_context=None): pass

    def on_end(self, span):
        try:
            size = encode_spans([span]).ByteSize()
            with self.lock:
                if self.stopping or size > 8192 or len(self.queue) >= 2048 or self.bytes + size > 8*1024*1024:
                    self.dropped += 1; return
                self.queue.append((span, size)); self.bytes += size; self.accepted += 1
                if len(self.queue) >= 128: self.lock.notify()
        except Exception:
            self.dropped += 1

    def _run(self):
        while True:
            with self.lock:
                if not self.stopping and len(self.queue) < 128: self.lock.wait(timeout=1)
                if self.stopping and not self.queue: return
                batch = []; size = 0
                while self.queue and len(batch) < 128 and size + self.queue[0][1] <= 480*1024:
                    item, length = self.queue.popleft(); self.bytes -= length
                    batch.append(item); size += length
                self.sending = bool(batch)
            if not batch: continue
            try:
                data = encode_spans(batch).SerializeToString()
                with self.session.post(self.endpoint, data=data, timeout=(1, 2), stream=True,
                    headers={'Content-Type': 'application/x-protobuf'}, allow_redirects=False) as response:
                    if not 200 <= response.status_code < 300: raise RuntimeError('otlp_http')
                    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
                    # Bounded response: OTLP partial-success counters, no server text.
                    content = response.raw.read(8193)
                    if len(content) > 8192: raise RuntimeError('otlp_response_bound')
                    rejected = ExportTraceServiceResponse.FromString(content).partial_success.rejected_spans if content else 0
                self.partial += rejected; self.exported += max(0, len(batch)-rejected)
            except Exception as exc:
                # Safe class only; exception messages may contain credentials.
                self.last_failure = type(exc).__name__
                self.failed += len(batch)
            finally:
                with self.lock: self.sending = False; self.lock.notify_all()

    def force_flush(self, timeout_millis=2000):
        deadline = time.monotonic()+min(timeout_millis, 2000)/1000
        with self.lock:
            self.lock.notify_all()
            while self.queue or self.sending:
                remaining = deadline-time.monotonic()
                if remaining <= 0: return False
                self.lock.wait(remaining)
        return True

    def shutdown(self):
        with self.lock:
            if self.stopping: return
        self.force_flush(2000)
        with self.lock:
            self.stopping = True; self.dropped += len(self.queue)
            self.queue.clear(); self.bytes = 0; self.lock.notify_all()


def configure(service):
    global _provider, _processor, _state
    if _provider is not None: return
    if os.environ.get('PHONOTEKA_TRACING_ENABLED', '').lower() not in {'true', '1'}: return
    try:
        endpoint = os.environ['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT']
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != '/v1/traces':
            raise ValueError('otlp_endpoint')
        # Separate service credentials. No env-var secret values or TLS fallback.
        import requests
        session = requests.Session(); session.trust_env = False
        session.verify = str(Path(os.environ['OTEL_EXPORTER_OTLP_CERTIFICATE']))
        session.cert = (os.environ['OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE'], os.environ['OTEL_EXPORTER_OTLP_CLIENT_KEY'])
        token = Path(os.environ['OTEL_EXPORTER_OTLP_TOKEN_FILE']).read_text().strip()
        if len(token) < 24: raise ValueError('otlp_token')
        session.headers['Authorization'] = 'Bearer '+token
        for path in (session.verify, *session.cert):
            if not Path(path).is_file(): raise ValueError('otlp_certificate')
        _processor = BoundedProcessor(endpoint, session)
        from . import __version__
        _provider = TracerProvider(sampler=ALWAYS_ON,
            resource=Resource({'service.namespace': 'phonoteka', 'service.name': 'phonoteka-'+service,
                'service.version': __version__, 'deployment.environment.name': 'homelab',
                'host.name': os.environ.get('OTEL_HOST_NAME', 'st1'), 'service.instance.id': str(uuid.uuid4())}),
            span_limits=SpanLimits(max_attributes=32, max_events=8, max_links=8, max_attribute_length=256),
            shutdown_on_exit=False)
        _provider.add_span_processor(_processor)
        install_network()
        atexit.register(shutdown)
        _state = 'enabled'
    except Exception:
        _state = 'configuration_error'
        from .telemetry import log
        log('tracing_configuration_error', code='tracing_configuration_error')


def shutdown():
    if _processor: _processor.shutdown()


def diagnostics():
    result = {'state': _state}
    if _processor:
        with _processor.lock:
            result.update(queued=len(_processor.queue), queued_bytes=_processor.bytes,
                accepted=_processor.accepted, exported=_processor.exported, dropped=_processor.dropped,
                failed=_processor.failed, partial=_processor.partial, last_failure=_processor.last_failure)
    return result


def correlation():
    value = trace.get_current_span().get_span_context()
    return {'trace_id': format(value.trace_id, '032x'), 'span_id': format(value.span_id, '016x')} if value.is_valid else {}


def grafana_link(identity):
    if not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{32}',identity): return None
    origin = os.environ.get('GRAFANA_TRACE_ORIGIN','')
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        return None
    import json
    from urllib.parse import urlencode
    uid = os.environ.get('GRAFANA_TRACES_DATASOURCE_UID','phonoteka-jaeger')
    if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',uid): return None
    panes = {'trace': {'datasource':uid, 'queries':[{'refId':'A','queryType':'traceID','query':identity,
        'datasource':{'type':'jaeger','uid':uid}}], 'range':{'from':'now-7d','to':'now'}}}
    return origin.rstrip('/')+'/explore?'+urlencode({'schemaVersion':1,'orgId':1,'panes':json.dumps(panes,separators=(',',':'))})


def carrier():
    value = {}; PROPAGATOR.inject(value)
    return {key: value[key] for key in ('traceparent',) if key in value}


def extracted(value):
    # Only an internally generated W3C parent; no tracestate or baggage accepted.
    parent = value.get('traceparent', '') if isinstance(value, dict) else ''
    if not re.fullmatch(r'00-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}', parent): return otcontext.Context()
    return PROPAGATOR.extract({'traceparent': parent})


def start(name, values=None, parent=None, kind=SpanKind.INTERNAL, links=()):
    state = SEGMENT.get()
    if state:
        with state['lock']:
            return _start(name, values, parent, kind, links)
    return _start(name, values, parent, kind, links)


def _start(name, values=None, parent=None, kind=SpanKind.INTERNAL, links=()):
    if _provider is None: return trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)
    if name != 'file.attempt':
        segment = segment_context()
        if segment:
            candidate = parent if parent is not None else otcontext.get_current()
            current = trace.get_current_span(candidate).get_span_context()
            replacement = trace.get_current_span(segment).get_span_context()
            # Preserve nesting inside the new segment, but never attach newly
            # created spans to a parent belonging to the previous segment.
            if current.trace_id != replacement.trace_id: parent = segment
    inner = _provider.get_tracer('phonoteka', '1').start_span(name, context=parent,
        kind=kind, attributes=safe(values or {}), links=links,
        record_exception=False, set_status_on_exception=False)
    state = SEGMENT.get()
    if not state: return inner
    with state['lock']:
        current = SegmentSpan(inner, state)
        state['active'].append(current)
        if name == 'file.attempt':
            state['root'] = current
            state['deadline_ns'] = inner.start_time + int(SEGMENT_SECONDS * 1_000_000_000)
            state['timer'] = threading.Timer(SEGMENT_SECONDS, expire_segment, args=(state, state['number']))
            state['timer'].daemon = True
            state['timer'].start()
        return current


@contextmanager
def span(name, values=None, parent=None, kind=SpanKind.INTERNAL, links=()):
    current = start(name, values, parent, kind, links)
    with trace.use_span(current, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        try: yield current
        except BaseException as exc:
            current.set_attribute('error.type', type(exc).__name__)
            if type(exc).__name__ != 'CancelledError': current.set_status(Status(StatusCode.ERROR))
            raise


@contextmanager
def file_attempt(job_id, item_id, attempt_id, number=None, previous=None):
    links = []
    prior = trace.get_current_span(extracted(previous or {})).get_span_context()
    if prior.is_valid: links = [Link(prior)]
    values = attributes(job_id=job_id, item_id=item_id, attempt_id=attempt_id, attempt_number=number)
    token = SEGMENT.set({'count': 1, 'started': time.monotonic(), 'values': values, 'number': 1, 'extra': None,
                         'lock': threading.RLock(), 'active': [], 'expired': False, 'finished': False})
    try:
        with span('file.attempt', values, parent=otcontext.Context(), links=links) as root:
            yield root
    finally:
        state = SEGMENT.get()
        with state['lock']:
            state['finished'] = True
            if state.get('timer'): state['timer'].cancel()
            for current in list(reversed(state['active'])): current.end()
        SEGMENT.reset(token)


def segment_context():
    """Conservative 1 MiB span budget and deadline-bounded partial intervals.

    Every exported span is capped at 8 KiB, hence 128 spans bound payload bytes.
    A deadline closes active intervals; the next operation links a new segment.
    """
    state = SEGMENT.get()
    if not state: return None
    with state['lock']:
        if state['expired'] or state['count'] >= 128 or time.monotonic()-state['started'] >= SEGMENT_SECONDS:
            prior = state['root'].get_span_context()
            state['timer'].cancel()
            for current in list(reversed(state['active'])):
                current.set_attribute('phonoteka.reason_code', 'segment_boundary')
                current.end(end_time=min(time.time_ns(), state['deadline_ns']))
            state['number'] += 1
            state['expired'] = False
            state['extra'] = start('file.attempt', {**state['values'], 'phonoteka.segment.number': state['number']},
                parent=otcontext.Context(), links=[Link(prior)] if prior.is_valid else [])
            state['count'] = 1; state['started'] = time.monotonic()
        state['count'] += 1
        if state['extra']: return trace.set_span_in_context(state['extra'])
    return None


def host(value):
    if isinstance(value, bytes): value = value.decode('ascii', 'ignore')
    return value if isinstance(value, str) and HOST.fullmatch(value) else 'unknown'


def http_route(url):
    """Known endpoint templates only; arbitrary IDs/path segments never escape."""
    path = url.path
    if url.host == 'musicbrainz.org':
        match = re.fullmatch(r'/ws/2/(recording|release|release-group|artist)(/[a-f0-9-]{36})?/?',path)
        if match: return '/ws/2/'+match[1]+('/{id}' if match[2] else '')
    if url.host in {'lrclib.net','lrclib.com'} and path in {'/api/search','/api/get','/api/get-cached'}: return path
    if url.host == 'amp.shazam.com' and path.startswith('/discovery/v5/'): return '/discovery/v5/{client}/{request}'
    if url.host == 'coverartarchive.org': return '/release/{id}/{image}'
    if path.endswith('/audio/transcriptions'): return '/audio/transcriptions'
    if path.endswith('/audio/align'): return '/audio/align'
    internal = urlsplit(os.environ.get('INFERENCE_URL',''))
    if internal.hostname == url.host:
        if path == '/jobs' or path == '/health': return path
        if re.fullmatch('/jobs/[a-f0-9]{64}',path): return '/jobs/{id}'
    return 'unclassified'


def active(): return _provider is not None and trace.get_current_span().is_recording()


def install_network():
    """HTTPX actual exchanges + real resolver calls, safe for concurrent contexts."""
    global _patched
    if _patched: return
    _patched = True
    original_dns = socket.getaddrinfo
    def dns(name, port, *args, **kwargs):
        if not active(): return original_dns(name, port, *args, **kwargs)
        with span('dns.lookup', {'dns.question.name': host(name), 'server.port': int(port) if str(port).isdigit() else 0,
                                'dns.question.type': 'A/AAAA'}) as current:
            result = original_dns(name, port, *args, **kwargs)
            current.set_attribute('dns.answers.count', len(result)); return result
    socket.getaddrinfo = dns
    original_async_dns = asyncio.BaseEventLoop.getaddrinfo
    async def async_dns(self, name, port, *args, **kwargs):
        if not active(): return await original_async_dns(self, name, port, *args, **kwargs)
        with span('dns.lookup', {'dns.question.name': host(name), 'dns.question.type': 'A/AAAA'}) as current:
            result = await original_async_dns(self, name, port, *args, **kwargs)
            current.set_attribute('dns.answers.count', len(result)); return result
    asyncio.BaseEventLoop.getaddrinfo = async_dns
    import httpx
    sync_send, async_send = httpx.Client._send_single_request, httpx.AsyncClient._send_single_request
    class SyncStream(httpx.SyncByteStream):
        def __init__(self, stream, current): self.stream, self.current, self.ended = stream, current, False
        def finish(self):
            if not self.ended: self.ended=True; self.current.end()
        def __iter__(self):
            with trace.use_span(self.current, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                try: yield from self.stream
                except BaseException as exc:
                    self.current.set_attribute('error.type',type(exc).__name__)
                    self.current.set_status(Status(StatusCode.ERROR)); raise
                finally: self.finish()
        def close(self):
            try: self.stream.close()
            finally: self.finish()
    class AsyncStream(httpx.AsyncByteStream):
        def __init__(self, stream, current): self.stream, self.current, self.ended = stream, current, False
        def finish(self):
            if not self.ended: self.ended=True; self.current.end()
        async def __aiter__(self):
            with trace.use_span(self.current, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                try:
                    async for block in self.stream: yield block
                except BaseException as exc:
                    self.current.set_attribute('error.type',type(exc).__name__)
                    self.current.set_status(Status(StatusCode.ERROR)); raise
                finally: self.finish()
        async def aclose(self):
            try: await self.stream.aclose()
            finally: self.finish()
    def internal_headers(request):
        internal = urlsplit(os.environ.get('INFERENCE_URL', ''))
        trusted = internal.hostname and (request.url.scheme, request.url.host, request.url.port) == (internal.scheme, internal.hostname, internal.port)
        for key in ('traceparent','tracestate','baggage'):
            request.headers.pop(key, None)
        if trusted: request.headers.update(carrier())
        else:
            for key in ('x-phonoteka-job-id','x-phonoteka-item-id'): request.headers.pop(key,None)
    def request_values(request):
        return {'http.request.method': request.method if request.method in {'GET','POST','PUT','PATCH','HEAD','DELETE','OPTIONS'} else 'OTHER',
                'server.address': host(request.url.host), 'server.port': request.url.port or (443 if request.url.scheme == 'https' else 80),
                'url.scheme': request.url.scheme, 'http.route':http_route(request.url)}
    def send(self, request):
        if not active(): return sync_send(self, request)
        current = start('http.client', request_values(request), kind=SpanKind.CLIENT)
        with trace.use_span(current, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            try:
                internal_headers(request); install_httpcore_trace(request)
                response = sync_send(self, request)
                current.set_attribute('http.response.status_code', response.status_code)
                if response.status_code >= 400: current.set_status(Status(StatusCode.ERROR))
                response.stream = SyncStream(response.stream, current)
                return response
            except BaseException as exc:
                current.set_attribute('error.type',type(exc).__name__)
                current.set_status(Status(StatusCode.ERROR)); current.end(); raise
    async def asend(self, request):
        if not active(): return await async_send(self, request)
        current = start('http.client', request_values(request), kind=SpanKind.CLIENT)
        with trace.use_span(current, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            try:
                internal_headers(request); install_httpcore_trace(request, asynchronous=True)
                response = await async_send(self, request)
                current.set_attribute('http.response.status_code', response.status_code)
                if response.status_code >= 400: current.set_status(Status(StatusCode.ERROR))
                response.stream = AsyncStream(response.stream, current)
                return response
            except BaseException as exc:
                current.set_attribute('error.type',type(exc).__name__)
                current.set_status(Status(StatusCode.ERROR)); current.end(); raise
    httpx.Client._send_single_request = send
    httpx.AsyncClient._send_single_request = asend
    # ShazamIO uses aiohttp rather than HTTPX. Its request coroutine ends once
    # response headers arrive; body reads happen later and are not guessed here.
    import aiohttp
    aiohttp_request = aiohttp.ClientSession._request
    async def aio_request(self, method, url, *args, **kwargs):
        if not active(): return await aiohttp_request(self, method, url, *args, **kwargs)
        from yarl import URL
        target = URL(url)
        values = {'http.request.method': str(method).upper()[:16], 'server.address': host(target.host),
            'server.port': target.port or (443 if target.scheme == 'https' else 80),
            'url.scheme': target.scheme, 'http.route': http_route(target),
            'phonoteka.operation': 'response_headers'}
        with span('http.client', values, kind=SpanKind.CLIENT) as current:
            response = await aiohttp_request(self, method, url, *args, **kwargs)
            current.set_attribute('http.response.status_code', response.status)
            if response.status >= 400: current.set_status(Status(StatusCode.ERROR))
            return response
    aiohttp.ClientSession._request = aio_request


def install_httpcore_trace(request, asynchronous=False):
    """httpcore supplies connect/TLS/write/read events without request capture."""
    previous = request.extensions.get('trace'); pending = {}
    if getattr(previous,'_phonoteka_trace',False): previous = previous._original_trace
    parent = otcontext.get_current()
    names = {'connection.connect_tcp': 'network.connect', 'connection.start_tls': 'network.tls',
             'http11.send_request_headers': 'http.send_headers', 'http11.send_request_body': 'http.send_body',
             'http11.receive_response_headers': 'http.receive_headers', 'http11.receive_response_body': 'http.receive_body',
             'http2.send_request_headers': 'http.send_headers', 'http2.send_request_body': 'http.send_body',
             'http2.receive_response_headers': 'http.receive_headers', 'http2.receive_response_body': 'http.receive_body'}
    def event(name, info):
        base, _, ending = name.rpartition('.')
        if base not in names: return
        if ending == 'started':
            child = start(names[base], {'network.transport': 'tcp'}, parent=parent)
            token = otcontext.attach(trace.set_span_in_context(child))
            pending[base] = (child, token)
        elif base in pending:
            child, token = pending.pop(base)
            if ending == 'failed':
                child.set_attribute('error.type', type(info.get('exception')).__name__)
                child.set_status(Status(StatusCode.ERROR))
            child.end(); otcontext.detach(token)
    if asynchronous:
        async def callback(name, info):
            event(name, info)
            if previous: await previous(name, info)
    else:
        def callback(name, info):
            event(name, info)
            if previous: previous(name, info)
    callback._phonoteka_trace = True
    callback._original_trace = previous
    request.extensions['trace'] = callback
