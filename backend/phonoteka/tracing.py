"""Opt-in, bounded OTel tracing. Never exports paths, query strings or bodies.

No automatic instrumentation: its default URL/exception capture is inappropriate
for music/provider credentials. Only the allowlisted attributes below cross OTLP.
"""
import asyncio
import atexit
import heapq
import itertools
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import re
import socket
import sys
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
from opentelemetry.exporter.otlp.proto.common._internal.trace_encoder import _encode_span

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
_cached_provider = None
_cached_tracer = None
_processor = None
_patched = False
_state = 'disabled'
SEGMENT_SECONDS = 900
_deadlines = []
_deadline_condition = threading.Condition()
_deadline_sequence = itertools.count()
_deadline_thread = None


class ServiceResource(Resource):
    """The service's resource attributes and schema are fixed at startup."""
    def __init__(self, attributes):
        super().__init__(attributes)
        # The pinned SDK groups each span by resource, computing its hash by
        # serializing its attributes as JSON. Preserve its equality/hash contract.
        self._service_hash = super().__hash__()

    def __hash__(self):
        return self._service_hash


class SegmentDeadline:
    """One scheduler per process; cancelled entries release attempt state."""
    def __init__(self, state):
        global _deadline_thread
        self.state, self.number = state, state['number']
        with _deadline_condition:
            # The scheduler may still be sleeping until a cancelled head's
            # earlier deadline. Keep that wake-up bound before pruning: moving
            # the head later never needs a notification; inserting an earlier
            # live deadline still does.
            earliest = _deadlines[0][0] if _deadlines else float('inf')
            while _deadlines and _deadlines[0][2].state is None:
                heapq.heappop(_deadlines)
            heapq.heappush(_deadlines, (state['started'] + SEGMENT_SECONDS,
                                     next(_deadline_sequence), self))
            if _deadline_thread is None:
                _deadline_thread = threading.Thread(target=_run_deadlines, daemon=True,
                                                    name='otel-segment-deadlines')
                _deadline_thread.start()
            if state['started'] + SEGMENT_SECONDS < earliest:
                _deadline_condition.notify()

    def cancel(self):
        with _deadline_condition:
            self.state = None
            # Release the attempt immediately. Its cancelled timer cannot make
            # a live deadline late; the next admission or scheduled wake prunes
            # it without waking another Python thread on every file ending.


def _run_deadlines():
    while True:
        with _deadline_condition:
            while _deadlines and _deadlines[0][2].state is None:
                heapq.heappop(_deadlines)
            if not _deadlines:
                _deadline_condition.wait()
                continue
            deadline, _, entry = _deadlines[0]
            remaining = deadline - time.monotonic()
            if remaining > 0:
                _deadline_condition.wait(remaining)
                continue
            heapq.heappop(_deadlines)
            state, entry.state = entry.state, None
        # Never take the attempt lock while holding the scheduler condition.
        if state is not None:
            expire_segment(state, entry.number)


class SegmentSpan(trace.Span):
    """Idempotent SDK span facade shared by application and deadline thread."""
    def __init__(self, inner, state):
        self.inner, self.state, self.closed = inner, state, False

    def get_span_context(self):
        with self.state['lock']: return self.inner.get_span_context()
    def is_recording(self):
        with self.state['lock']: return not self.closed and self.inner.is_recording()
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
            if not self.state['finished'] and time.monotonic()-self.state['started'] >= SEGMENT_SECONDS:
                rotate_segment(self.state, deadline=True)
            self.closed = True
            self.inner.end(end_time=end_time)
            self.state['active'].remove(self)


def expire_segment(state, number):
    """Continue open logical spans without repeating their business operation."""
    with state['lock']:
        if state['finished'] or state['number'] != number: return
        rotate_segment(state, deadline=True)


def rotate_segment(state, deadline=False):
    # Called with the attempt lock held, including by the scheduler thread.
    state['timer'].cancel()
    boundary = state['deadline_ns'] if deadline else min(time.time_ns(), state['deadline_ns'])
    previous = [(current, current.inner, safe(dict(current.inner.attributes))) for current in state['active']]
    for _, inner, _ in reversed(previous):
        inner.set_attribute('phonoteka.reason_code', 'segment_deadline' if deadline else 'segment_boundary')
        inner.end(end_time=boundary)
    state['number'] += 1
    now = time.monotonic()
    scheduled = state['started'] + SEGMENT_SECONDS if deadline else now
    # A suspended process cannot produce an unbounded catch-up burst. Make the
    # unobserved interval explicit instead of fabricating many timed segments.
    delayed = now - scheduled >= SEGMENT_SECONDS
    state['started'] = now if delayed else scheduled
    start_ns = time.time_ns() if delayed else boundary
    parents = {}
    tracer = sdk_tracer()
    for current, old, values in previous:
        old_context = old.get_span_context()
        parent = parents.get(old.parent.span_id, state['root']) if old.parent else None
        values['phonoteka.segment.number'] = state['number']
        if delayed: values['phonoteka.reason_code'] = 'scheduler_delay'
        current.inner = tracer.start_span(old.name,
            context=trace.set_span_in_context(parent) if parent else otcontext.Context(),
            kind=old.kind, attributes=values, links=[Link(old_context)], start_time=start_ns,
            record_exception=False, set_status_on_exception=False)
        if old.status.status_code != StatusCode.UNSET: current.inner.set_status(old.status)
        parents[old_context.span_id] = current
    state['count'] = len(previous)
    state['deadline_ns'] = start_ns + int(SEGMENT_SECONDS * 1_000_000_000)
    state['timer'] = SegmentDeadline(state)


def safe(attributes):
    result = {}
    for key, value in attributes.items():
        if key not in ALLOWED or value is None: continue
        if key in IDS and not (isinstance(value, str) and OPAQUE.fullmatch(value)): continue
        if isinstance(value, str) and not (type(value) is str and value.isascii() and len(value) <= 256):
            value = value.encode('utf-8')[:256].decode('utf-8', 'ignore')
        if type(value) in (str, bool, int, float): result[key] = value
    return result


def attributes(**values):
    return {'phonoteka.' + key.replace('_', '.'): value for key, value in values.items()}


def _wire_batches(encoded):
    """Share OTLP resource/scope metadata without relaxing either wire bound.

    A small group fitting the individual limit proves every contained span fits.
    Larger groups use the previous exact single-envelope check. Metadata,
    including unknown protobuf fields, is copied unchanged.
    """
    # The SDK already produced a complete OTLP request. For ordinary short
    # spans it fits as-is, including every resource and instrumentation scope.
    if encoded.ByteSize() <= 8192:
        count = sum(len(scope.spans) for resource in encoded.resource_spans for scope in resource.scope_spans)
        return ([(encoded.SerializeToString(), count)] if count else []), 0
    packets = []; dropped = 0
    for resource in encoded.resource_spans:
        resource_meta = type(resource)(); resource_meta.CopyFrom(resource)
        scope_metas = []
        for copied_scope in resource_meta.scope_spans:
            copied_scope.ClearField('spans')
            scope_meta = type(copied_scope)(); scope_meta.CopyFrom(copied_scope)
            scope_metas.append(scope_meta)
        resource_meta.ClearField('scope_spans')
        for scope, scope_meta in zip(resource.scope_spans, scope_metas):
            envelope = type(encoded)()
            target = envelope.resource_spans.add(); target.CopyFrom(resource_meta)
            group = target.scope_spans.add(); group.CopyFrom(scope_meta)
            for offset in range(0, len(scope.spans), 16):
                items = scope.spans[offset:offset+16]
                group.spans.extend(items)
                data = envelope.SerializeToString()
                if len(data) <= 8192:
                    packets.append((data, len(items)))
                    group.ClearField('spans'); continue
                group.ClearField('spans')
                for item in items:
                    group.spans.add().CopyFrom(item)
                    data = envelope.SerializeToString(); group.ClearField('spans')
                    if len(data) > 8192: dropped += 1
                    else: packets.append((data, 1))
    return packets, dropped


def _encode_export_batch(batch):
    """Reuse the pinned SDK's resource envelope within one bounded export."""
    template = cached_resource = cached_scope = None
    packets = []
    dropped = 0
    for offset in range(0, len(batch), 16):
        group = batch[offset:offset + 16]
        first = group[0]
        resource = getattr(first, 'resource', None)
        scope = getattr(first, 'instrumentation_scope', None)
        shared = (isinstance(resource, ServiceResource) and scope is not None
                  and not scope.attributes and all(span.resource is resource
                  and span.instrumentation_scope is scope for span in group))
        if shared and resource is cached_resource and scope is cached_scope:
            template.resource_spans[0].scope_spans[0].spans.extend(_encode_span(span) for span in group)
            encoded = template
        else:
            encoded = encode_spans(group)
        part, oversized = _wire_batches(encoded)
        packets.extend(part)
        dropped += oversized
        if offset + 16 < len(batch):
            if shared:
                template, cached_resource, cached_scope = encoded, resource, scope
                template.resource_spans[0].scope_spans[0].ClearField('spans')
            time.sleep(0)
    return packets, dropped


def _background_cpu_priority():
    # Linux nice is per native thread. Never renice the application process or
    # raise an inherited priority; restricted/other platforms keep exporting.
    if sys.platform != 'linux' or not hasattr(os, 'setpriority'): return
    try:
        identity = threading.get_native_id()
        current = os.getpriority(os.PRIO_PROCESS, identity)
        if current < 19: os.setpriority(os.PRIO_PROCESS, identity, min(19, current+5))
    except OSError:
        pass


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
        # Reserve the maximum allowed wire size before encoding. SDK on_end runs
        # on the business caller: serializing here stalls concurrent HTTP work.
        # This conservative reservation admits at most 1024 pending spans.
        with self.lock:
            if self.stopping or len(self.queue) >= 2048 or self.bytes + 8192 > 8*1024*1024:
                self.dropped += 1; return
            self.queue.append(span); self.bytes += 8192; self.accepted += 1
            if len(self.queue) == 128: self.lock.notify()

    def _send(self, data, count):
        try:
            with self.session.post(self.endpoint, data=data, timeout=(1, 2), stream=True,
                headers={'Content-Type': 'application/x-protobuf'}, allow_redirects=False) as response:
                if not 200 <= response.status_code < 300: raise RuntimeError('otlp_http')
                from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
                # Bounded response: OTLP partial-success counters, no server text.
                content = response.raw.read(8193)
                if len(content) > 8192: raise RuntimeError('otlp_response_bound')
                rejected = ExportTraceServiceResponse.FromString(content).partial_success.rejected_spans if content else 0
            self.partial += rejected; self.exported += max(0, count-rejected)
        except Exception as exc:
            self.last_failure = type(exc).__name__
            self.failed += count

    def _run(self):
        _background_cpu_priority()
        while True:
            with self.lock:
                if not self.stopping and len(self.queue) < 128: self.lock.wait(timeout=1)
                if self.stopping and not self.queue: return
                batch = []
                while self.queue and len(batch) < 128:
                    batch.append(self.queue.popleft()); self.bytes -= 8192
                self.sending = bool(batch)
            if not batch: continue
            try:
                chunks = []; size = count = 0
                try:
                    packets, rejected = _encode_export_batch(batch)
                except Exception:
                    self.dropped += len(batch); continue
                self.dropped += rejected
                for data, spans in packets:
                    if size+len(data) > 480*1024:
                        self._send(b''.join(chunks), count); chunks = []; size = count = 0
                    chunks.append(data); size += len(data); count += spans
                # ExportTraceServiceRequest has repeated resource_spans only.
                # Concatenation preserves distinct resource/scope packets.
                if chunks: self._send(b''.join(chunks), count)
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
            resource=ServiceResource({'service.namespace': 'phonoteka', 'service.name': 'phonoteka-'+service,
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


def sdk_tracer():
    """Instrumentation scope is process-wide, not a new object per HTTP phase."""
    global _cached_provider, _cached_tracer
    if _cached_provider is not _provider:
        _cached_tracer = _provider.get_tracer('phonoteka', '1')
        _cached_provider = _provider
    return _cached_tracer


def start(name, values=None, parent=None, kind=SpanKind.INTERNAL, links=()):
    if _provider is None: return trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)
    state = SEGMENT.get()
    if state:
        with state['lock']:
            return _start_in_segment(state, name, values, parent, kind, links)
    return _sdk_start(name, values, parent, kind, links)


def _sdk_start(name, values, parent, kind, links):
    return sdk_tracer().start_span(name, context=parent,
        kind=kind, attributes=safe(values or {}), links=links,
        record_exception=False, set_status_on_exception=False)


def _start_in_segment(state, name, values, parent, kind, links):
    # Called under the attempt lock. Rotation advances every open facade
    # together, so it already points to the current trace in the common case.
    if name != 'file.attempt':
        root = state['root']
        if len(state['active']) >= 128:
            if _processor is not None: _processor.dropped += 1
            return trace.NonRecordingSpan(root.inner.get_span_context())
        if time.monotonic()-state['started'] >= SEGMENT_SECONDS:
            rotate_segment(state, deadline=True)
        elif state['count'] >= 128:
            rotate_segment(state)
        state['count'] += 1
        candidate = trace.get_current_span(parent)
        if not (isinstance(candidate, SegmentSpan) and candidate.state is state and not candidate.closed):
            if candidate.get_span_context().trace_id != root.inner.get_span_context().trace_id:
                parent = trace.set_span_in_context(root)
    inner = _sdk_start(name, values, parent, kind, links)
    current = SegmentSpan(inner, state)
    state['active'].append(current)
    if name == 'file.attempt':
        state['root'] = current
        state['started'] = time.monotonic()
        state['deadline_ns'] = inner.start_time + int(SEGMENT_SECONDS * 1_000_000_000)
        state['timer'] = SegmentDeadline(state)
    return current


@contextmanager
def span(name, values=None, parent=None, kind=SpanKind.INTERNAL, links=()):
    current = start(name, values, parent, kind, links)
    token = otcontext.attach(trace.set_span_in_context(current))
    try:
        yield current
    except BaseException as exc:
        current.set_attribute('error.type', type(exc).__name__)
        if type(exc).__name__ != 'CancelledError': current.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        otcontext.detach(token)
        current.end()


@contextmanager
def file_attempt(job_id, item_id, attempt_id, number=None, previous=None):
    links = []
    prior = trace.get_current_span(extracted(previous or {})).get_span_context()
    if prior.is_valid: links = [Link(prior)]
    values = attributes(job_id=job_id, item_id=item_id, attempt_id=attempt_id, attempt_number=number)
    token = SEGMENT.set({'count': 1, 'started': time.monotonic(), 'values': values, 'number': 1,
                         'lock': threading.RLock(), 'active': [], 'finished': False})
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
