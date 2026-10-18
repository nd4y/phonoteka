"""Bounded Prometheus instrumentation and privacy-safe JSON stdout logging."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import functools
import hmac
import json
import logging
import os
import re
from pathlib import Path
import sys
import time
import traceback
import uuid

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, GCCollector, PlatformCollector, ProcessCollector, generate_latest, CONTENT_TYPE_LATEST
from . import __version__

CONTEXT = ContextVar('telemetry_context', default={})
FIELDS = {'request_id','job_id','track_id','kind','state','code','provider','operation','status','method','route','duration_seconds','count','completed','failed','total','retry_at','error_type','attempt','port'}
KINDS = ('scan','audit','apply','rollback','publish','analyze','recognize','transcribe','align','extract_cue','replaygain','replaygain_album','export_ready','normalize_audio','youtube')
STATES = ('queued','running','paused','succeeded','failed','completed_with_errors','interrupted','cancelled','cancelled_partial')
PROVIDERS = ('musicbrainz','lrclib','coverartarchive','shazam','youtube','acoustid','theaudiodb','discogs')


def bounded(value, allowed):
    return value if value in allowed else 'other'


def opaque_id(value):
    return value if isinstance(value,str) and re.fullmatch(r'(?:[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})',value) else None


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__(); self.service = service

    def format(self, record):
        event = getattr(record, 'event', None)
        # Library message arguments and exception messages may contain URLs,
        # passwords, request bodies or filenames. They never enter the journal.
        data = {'timestamp':datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds'),
                'level':record.levelname.lower(),'service':self.service,'version':__version__,
                'logger':record.name,'event':event or 'library_log'}
        for key, value in {**CONTEXT.get(), **getattr(record, 'fields', {})}.items():
            if key in FIELDS and isinstance(value, (str,int,float,bool,type(None))):
                data[key] = value[:200] if isinstance(value,str) else value
        if record.exc_info and record.exc_info[0]:
            data['error_type'] = record.exc_info[0].__name__
            data['stack'] = [{'module':Path(f.filename).name,'function':f.name,'line':f.lineno}
                             for f in traceback.extract_tb(record.exc_info[2])[-12:]]
        return json.dumps(data, ensure_ascii=False, allow_nan=False)


def configure_logging(service):
    handler = logging.StreamHandler(sys.stdout); handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger(); root.handlers[:] = [handler]
    level = os.environ.get('LOG_LEVEL','INFO').upper()
    root.setLevel(level if level in {'DEBUG','INFO','WARNING','ERROR','CRITICAL'} else 'INFO')
    for name in ('uvicorn','uvicorn.error','uvicorn.access'):
        logger = logging.getLogger(name); logger.handlers.clear(); logger.propagate = True
    logging.getLogger('uvicorn.access').disabled = True
    for name in ('httpx','httpcore','faster_whisper','shazamio','asyncio'):
        logging.getLogger(name).setLevel(logging.WARNING)


def log(event, level=logging.INFO, **fields):
    logging.getLogger('phonoteka').log(level, event, extra={'event':event,'fields':fields})


@contextmanager
def context(**fields):
    token = CONTEXT.set({**CONTEXT.get(), **fields})
    try: yield
    finally: CONTEXT.reset(token)


def metrics_authorized(headers, secret_file):
    if not secret_file: return False
    try:
        secret = Path(secret_file).read_text(encoding='utf-8').strip()
        return len(secret) >= 24 and hmac.compare_digest(headers.get('authorization','').encode('utf-8'), ('Bearer '+secret).encode('utf-8'))
    except (OSError,UnicodeError): return False


class Metrics:
    def __init__(self, service):
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry); PlatformCollector(registry=self.registry); GCCollector(registry=self.registry)
        Gauge('phonoteka_build_info','Running component and version',['component','version'],registry=self.registry).labels(service,__version__).set(1)
        self.http = Counter('phonoteka_http_requests_total','HTTP requests',['method','route','status'],registry=self.registry)
        self.http_duration = Histogram('phonoteka_http_request_duration_seconds','Full HTTP response duration',['method','route'],buckets=(.005,.02,.1,.5,1,3,10,30,120),registry=self.registry)
        self.http_active = Gauge('phonoteka_http_requests_in_progress','Active HTTP requests',registry=self.registry)
        self.items = Counter('phonoteka_item_attempts_total','Finished file processing attempts, including deferrals',['kind','state'],registry=self.registry)
        self.item_duration = Histogram('phonoteka_item_duration_seconds','File attempt duration',['kind'],buckets=(.1,1,5,15,30,60,120,300,900,1800),registry=self.registry)
        self.jobs_active = Gauge('phonoteka_jobs_in_progress','Current processing jobs',['kind'],registry=self.registry)
        self.providers = Counter('phonoteka_provider_operations_total','Logical provider operations, including cache hits',['provider','operation','outcome'],registry=self.registry)
        self.provider_duration = Histogram('phonoteka_provider_duration_seconds','Provider operation including throttle/retries',['provider','operation'],buckets=(.1,1,3,10,30,60,120),registry=self.registry)
        self.cache_hits = Counter('phonoteka_provider_cache_hits_total','Provider cache hits',['provider'],registry=self.registry)
        self.inference_jobs = Counter('phonoteka_inference_attempts_total','Inference executions',['state'],registry=self.registry)
        self.inference_duration = Histogram('phonoteka_inference_duration_seconds','ASR execution duration',buckets=(1,5,15,30,60,120,300,900,1800,3600),registry=self.registry)
        self.model_loaded = Gauge('phonoteka_inference_model_loaded','ASR model loaded in memory',registry=self.registry)
        for kind in KINDS:
            for state in ('succeeded','failed','needs_review','conflict','interrupted','deferred'): self.items.labels(kind,state)
        for state in ('succeeded','failed'): self.inference_jobs.labels(state)

    def render(self): return generate_latest(self.registry)

    @contextmanager
    def provider(self, provider, operation):
        provider = bounded(provider, PROVIDERS); started = time.perf_counter(); outcome = 'success'
        try: yield
        except BaseException as exc:
            outcome = bounded(getattr(exc,'code','error'), {'rate_limited','provider_unavailable','provider_cooldown','provider_unauthorized','budget_exhausted','provider_disabled'})
            if outcome == 'other': outcome = 'error'
            log('provider_operation_failed', logging.WARNING, provider=provider, operation=operation, code=outcome, error_type=type(exc).__name__)
            raise
        finally:
            duration = time.perf_counter()-started
            self.providers.labels(provider,operation,outcome).inc()
            self.provider_duration.labels(provider,operation).observe(duration)
            if outcome == 'success': log('provider_operation_finished',provider=provider,operation=operation,duration_seconds=round(duration,6))


class ObserveHTTP:
    """Pure ASGI middleware: includes streamed response time, no bodies/queries."""
    def __init__(self, app, metrics): self.app, self.metrics = app, metrics

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope,receive,send)
        if scope['path'] == '/health' or scope['path'].startswith('/metrics'):
            return await self.app(scope,receive,send)
        status = 500; started = time.perf_counter(); request_id = uuid.uuid4().hex
        async def observed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']; message = {**message,'headers':list(message.get('headers',[]))+[(b'x-request-id',request_id.encode())]}
            await send(message)
        self.metrics.http_active.inc()
        with context(request_id=request_id):
            try: await self.app(scope,receive,observed_send)
            except BaseException as exc:
                log('http_exception', logging.ERROR, error_type=type(exc).__name__)
                raise
            finally:
                method = bounded(scope['method'], {'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'})
                route = getattr(scope.get('route'),'path','unmatched')
                duration = time.perf_counter()-started
                self.metrics.http.labels(method,route,str(status)).inc(); self.metrics.http_duration.labels(method,route).observe(duration); self.metrics.http_active.dec()
                log('http_request', logging.ERROR if status>=500 else logging.WARNING if status>=400 else logging.INFO,
                    method=method,route=route,status=status,duration_seconds=round(duration,6))


def provider_operation(operation):
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapped(self, provider, *args, **kwargs):
            with self.metrics.provider(provider,operation): return await fn(self,provider,*args,**kwargs)
        return wrapped
    return decorate


def worker_server(metrics, token_file, port):
    """Private Docker-network endpoint; no music/auth APIs on this listener."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics': status,body = 404,b'Not found\n'
            elif not metrics_authorized(self.headers,token_file): status,body = 401,b'Unauthorized\n'
            else: status,body = 200,metrics.render()
            self.send_response(status); self.send_header('Content-Type',CONTENT_TYPE_LATEST); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*args): pass
    class Server(ThreadingHTTPServer):
        def handle_error(self,*args):
            log('metrics_http_error',logging.WARNING,error_type=type(sys.exc_info()[1]).__name__)
    server = Server(('0.0.0.0',port),Handler); server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,name='metrics-http',daemon=True); thread.start()
    log('metrics_listener_started',port=server.server_port)
    return server
