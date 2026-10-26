"""Best-effort, bounded stage measurements; never a second business queue.

The existing event retention applies. Missing transitions are explicitly partial,
not reconstructed durations. No file names, URLs, payloads or exception text.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import threading
import time
import uuid

from . import db, tracing
from opentelemetry import trace
from .telemetry import PROVIDERS

CURRENT = ContextVar('processing_attempt', default=None)
CURRENT_STAGE = ContextVar('processing_stage', default=None)
STAGES = {'file', 'preflight', 'fingerprint_sample', 'fingerprint_lookup', 'metadata_lookup',
          'decode', 'write_tags', 'commit', 'rollback', 'publication', 'cue_extract',
          'asr', 'align', 'normalize', 'sidecars', 'provider_lookup', 'provider_admission', 'provider_http'}
REASONS = {'provider_unavailable', 'provider_cooldown', 'rate_limited', 'budget_exhausted',
           'unknown_outcome', 'provider_policy_denied', 'provider_policy_expired',
           'provider_unauthorized', 'response_too_large', 'cancelled', 'conflict',
           'authorization_revoked', 'insufficient_space', 'internal_error'}


class Attempt:
    def __init__(self, path, job_id, item_id, executor):
        self.path, self.job_id, self.item_id = path, job_id, item_id
        self.identity, self.executor = str(uuid.uuid4()), executor
        self.action = None
        self.outcome = 'interrupted'
        self.lock = threading.RLock()
        self.partial = False
        self.root_id = None
        self.number = None
        self.previous = None

    def trace_history(self):
        if tracing.diagnostics()['state'] != 'enabled': return
        try:
            with db.connect(self.path) as conn:
                conn.execute('PRAGMA busy_timeout=20')
                row = conn.execute('SELECT number,context FROM trace_attempts WHERE job_id=? AND item_id=?',
                                   (self.job_id, self.item_id)).fetchone()
                if row:
                    import json
                    self.number, self.previous = row['number']+1, json.loads(row['context'])
                else: self.number = 1
        except Exception: self.partial = True

    def remember_trace(self):
        if self.number is None: return
        try:
            with db.connect(self.path) as conn:
                conn.execute('PRAGMA busy_timeout=20')
                conn.execute('INSERT OR REPLACE INTO trace_attempts VALUES(?,?,?,?,?)',
                    (self.job_id, self.item_id, self.number, db.encode(tracing.carrier()), db.utcnow()))
                conn.execute("DELETE FROM trace_attempts WHERE rowid IN (SELECT t.rowid FROM trace_attempts t JOIN jobs j ON j.id=t.job_id WHERE j.state NOT IN ('queued','running','paused','waiting_authorization') AND t.updated_at < strftime('%Y-%m-%dT%H:%M:%f+00:00','now','-14 days') LIMIT 100)")
        except Exception: self.partial = True

    def emit(self, value):
        # Telemetry has a short contention budget and cannot roll back media.
        try:
            with db.connect(self.path) as conn:
                conn.execute('PRAGMA busy_timeout=20')
                conn.execute('BEGIN IMMEDIATE')
                db.event(conn, 'job_stage', {**{k:v for k,v in value.items() if not k.startswith('_')}, 'job_id': self.job_id,
                    'track_id': self.item_id, 'attempt_id': self.identity,
                    'executor_instance_id': self.executor, 'history_partial': self.partial})
        except Exception:
            self.partial = True

    def begin(self, name, provider=None, parent=None):
        stage = {'stage_id': str(uuid.uuid4()), 'stage': name if name in STAGES else 'preflight',
                 'parent_stage_id': parent or self.root_id, 'provider': provider if provider in PROVIDERS else None,
                 'started_at': db.utcnow(), 'state': 'running', 'progress_observability': 'unavailable'}
        if name != 'file':
            current = CURRENT_STAGE.get()
            parent_span = current[1][0].get('_otel') if current and current[0] is self else self.action[0].get('_otel') if self.action else None
            parent_context = trace.set_span_in_context(parent_span) if parent_span else None
            canonical = 'provider.'+('caa' if provider=='coverartarchive' else provider) if name=='provider_lookup' and provider in PROVIDERS else {
                'decode':'audio.decode','fingerprint_sample':'fingerprint.compute','asr':'asr.transcribe',
                'write_tags':'metadata.write','commit':'result.verify'}.get(name,'file.stage')
            stage['_otel'] = tracing.start(canonical, tracing.attributes(stage=name, provider=provider),
                parent=tracing.segment_context() or parent_context)
            sc = stage['_otel'].get_span_context()
            if sc.is_valid: stage.update(trace_id=format(sc.trace_id,'032x'), span_id=format(sc.span_id,'016x'))
        else: stage.update(tracing.correlation())
        if self.number is not None: stage['attempt_number'] = self.number
        self.emit(stage)
        return stage, time.monotonic()

    def end(self, handle, state, reason=None, status=None):
        stage, started = handle
        otel = stage.get('_otel')
        if otel:
            otel.set_attribute('phonoteka.outcome', state)
            if reason in REASONS: otel.set_attribute('phonoteka.reason_code', reason)
            if type(status) is int: otel.set_attribute('http.response.status_code', status)
            if state == 'failed': otel.set_status(trace.Status(trace.StatusCode.ERROR))
            otel.end()
        self.emit({**stage, 'state': state, 'ended_at': db.utcnow(),
            'duration_ms': round(max(0, time.monotonic()-started)*1000, 3),
            'reason_code': reason if reason in REASONS else None,
            'http_status': status if type(status) is int and 100 <= status <= 599 else None})

    def phase(self, name):
        with self.lock:
            if self.action: self.end(self.action, 'succeeded')
            self.action = self.begin(name)

    def finish(self):
        with self.lock:
            if self.action:
                self.end(self.action, self.outcome)
                self.action = None


@contextmanager
def attempt(path, job_id, item_id, executor, enqueue_context=None):
    value = Attempt(path, job_id, item_id, executor)
    value.trace_history()
    token = CURRENT.set(value)
    try:
        with tracing.file_attempt(job_id, item_id, value.identity, value.number, value.previous or enqueue_context) as otel:
            value.remember_trace()
            root = value.begin('file')
            value.root_id = root[0]['stage_id']
            try: yield value
            finally:
                value.finish()
                value.end(root, value.outcome)
                otel.set_attribute('phonoteka.outcome', value.outcome)
    finally:
        CURRENT.reset(token)


def action(name):
    value = CURRENT.get()
    if value: value.phase(name)


def outcome(state):
    value = CURRENT.get()
    if value and state != 'running': value.outcome = state


@contextmanager
def stage(name, provider=None):
    value = CURRENT.get()
    if not value:
        yield {}
        return
    current = CURRENT_STAGE.get()
    parent = current[1][0]['stage_id'] if current and current[0] is value else value.action[0]['stage_id'] if value.action else value.root_id
    handle = value.begin(name, provider, parent)
    token = CURRENT_STAGE.set((value, handle))
    otel_token = tracing.otcontext.attach(trace.set_span_in_context(handle[0]['_otel'])) if handle[0].get('_otel') else None
    result = {}
    try:
        yield result
    except BaseException as exc:
        value.end(handle, 'cancelled' if type(exc).__name__ == 'CancelledError' else 'failed',
                  getattr(exc, 'code', None), result.get('http_status'))
        raise
    else:
        value.end(handle, 'succeeded', status=result.get('http_status'))
    finally:
        if otel_token is not None: tracing.otcontext.detach(otel_token)
        CURRENT_STAGE.reset(token)


def checkpoint(counter, unit):
    """Call only for actual work, never for polling or heartbeat."""
    current = CURRENT_STAGE.get()
    if not current or type(counter) is not int or counter < 0 or unit not in {'bytes', 'responses', 'items'}: return
    value, handle = current
    data = handle[0]
    with value.lock:
        if data.get('progress_unit') == unit and counter <= data.get('progress_counter', -1): return
        previous = data.get('_checkpoint_clock', 0)
        if data.get('progress_unit') == unit and time.monotonic()-previous < 5: return
        data.update(progress_counter=counter, progress_unit=unit, last_progress_at=db.utcnow(),
                    progress_observability='counter')
        value.emit({k:v for k,v in data.items() if not k.startswith('_')})
        data['_checkpoint_clock'] = time.monotonic()


def provider_lookup(fn):
    @wraps(fn)
    async def measured(self, provider, *args, **kwargs):
        with stage('provider_lookup', provider):
            return await fn(self, provider, *args, **kwargs)
    return measured


def project(events, rows, heartbeat, now, *, job_running=True):
    """An explicit owner-only projection of retained measurements, not raw data."""
    from datetime import datetime
    def epoch(value):
        try: return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (AttributeError, TypeError, ValueError): return None
    def allowed(value, values): return value if isinstance(value, str) and value in values else None
    latest, stages = {}, {}
    for event in events:
        if event['kind'] != 'job_stage': continue
        data = event['data']
        if not allowed(data.get('stage'), STAGES): continue
        identity = data.get('stage_id')
        if not isinstance(identity, str) or len(identity) > 64: continue
        # Only identifiers generated by this module may enter the projection.
        if any(not isinstance(data.get(k), str) or len(data[k]) != 36
               for k in ('track_id', 'attempt_id', 'executor_instance_id')): continue
        safe = {k: data.get(k) for k in ('track_id', 'attempt_id', 'stage_id', 'stage',
                    'parent_stage_id', 'executor_instance_id')}
        import re
        for key, length in (('trace_id',32),('span_id',16)):
            value = data.get(key)
            if isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(length)+'}',value): safe[key] = value
        if safe.get('trace_id'):
            safe['trace_url'] = tracing.grafana_link(safe['trace_id'])
        safe.update(provider=allowed(data.get('provider'), PROVIDERS),
            started_at=data.get('started_at') if epoch(data.get('started_at')) is not None else None,
            ended_at=data.get('ended_at') if epoch(data.get('ended_at')) is not None else None,
            state=allowed(data.get('state'), {'running','succeeded','failed','cancelled','interrupted','deferred','conflict','needs_review'}) or 'interrupted',
            reason_code=allowed(data.get('reason_code'), REASONS),
            http_status=data.get('http_status') if type(data.get('http_status')) is int and 100 <= data['http_status'] <= 599 else None,
            duration_ms=data.get('duration_ms') if type(data.get('duration_ms')) in (int,float) and 0 <= data['duration_ms'] < 1e12 else None,
            history_partial=bool(data.get('history_partial')), progress_observability='unavailable', sequence=event['id'])
        stages[identity] = safe
        if type(data.get('progress_counter')) is int and data['progress_counter'] >= 0 and data.get('progress_unit') in ('bytes','responses','items'):
            safe.update(progress_counter=data['progress_counter'], progress_unit=data['progress_unit'],
                        last_progress_at=data.get('last_progress_at') if epoch(data.get('last_progress_at')) is not None else None,
                        progress_observability='counter')
        if data['stage'] == 'file' and data.get('state') == 'running' and (
                heartbeat.get('activity_generation') is None or heartbeat['activity_generation']==data['executor_instance_id']):
            latest[data['track_id']] = data['attempt_id']
    at = heartbeat.get('at')
    age = max(0, now-at) if type(at) in (int,float) else None
    executor_health = 'unknown' if age is None else 'executor_unavailable' if age > 60 else 'heartbeat_stale' if age > 30 else 'responding'
    running = {row['item_key'] for row in rows if row['state'] == 'running'} if job_running else set()
    for value in stages.values():
        beginning = epoch(value['started_at'])
        value['elapsed_seconds'] = max(0, now-beginning) if beginning is not None and value['state']=='running' else None
        # Old processes never create a current-running assertion after restart.
        if value['state']=='running' and (value['track_id'] not in running or
                (heartbeat.get('activity_generation') is not None and heartbeat['activity_generation'] != value['executor_instance_id']) or
                latest.get(value['track_id']) != value['attempt_id']):
            value.update(state='interrupted', duration_ms=None, elapsed_seconds=None, history_partial=True)
        value['activity_health'] = executor_health
        threshold = 180 if value['stage'] in {'decode','fingerprint_sample','fingerprint_lookup'} else 60 if value['stage'] in {'preflight','write_tags','commit','rollback','publication'} else None
        value['soft_threshold_seconds'] = threshold
        value['long_running'] = bool(threshold is not None and value['state']=='running' and value['elapsed_seconds'] is not None and value['elapsed_seconds'] > threshold)
        progress_at = epoch(value.get('last_progress_at'))
        value['last_progress_age_seconds'] = max(0, now-progress_at) if progress_at is not None else None
        value['no_progress_suspected'] = bool(value['state']=='running' and executor_health=='responding' and progress_at is not None and now-progress_at>120)
    ordered = sorted(stages.values(), key=lambda value: value['sequence'])
    current = [value for value in ordered if value['state']=='running' and value['stage']!='file']
    return {'server_now': now, 'executor_health': executor_health, 'heartbeat_age_seconds': age,
            'progress_observability': 'partial' if any(value['progress_observability']=='counter' for value in current) else 'unavailable', 'history_completeness': 'bounded',
            'current_stages': current[-100:], 'stage_history': ordered[-100:],
            'stages_truncated': len(ordered)>100}
