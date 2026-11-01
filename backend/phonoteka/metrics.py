"""Read-only, aggregate database collectors. No paths, titles, IDs or lyrics."""
from contextlib import closing
import json
import logging
import sqlite3
import threading
import time
from prometheus_client.core import GaugeMetricFamily
from .telemetry import KINDS, STATES, PROVIDERS, bounded, log


def gauge(name, help, rows, labels=()):
    metric = GaugeMetricFamily('phonoteka_'+name, help, labels=list(labels))
    for keys,value in rows: metric.add_metric(list(keys),value)
    return metric


class DatabaseCollector:
    def __init__(self, settings=None, inference_path=None):
        self.settings, self.inference_path = settings, inference_path
        self.lock = threading.Lock(); self.cached = []; self.checked = 0; self.success = 0

    def describe(self): return []

    def collect(self):
        with self.lock:
            now = time.time()
            if now-self.checked >= 5:
                self.checked = now
                try:
                    path = self.inference_path or self.settings.db_path
                    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)) as conn:
                        conn.row_factory = sqlite3.Row; conn.execute('PRAGMA query_only=ON'); conn.execute('BEGIN')
                        self.cached = self.inference(conn,now) if self.inference_path else self.application(conn,now)
                    self.success = now
                except Exception as exc:
                    log('metrics_collection_failed',logging.WARNING,error_type=type(exc).__name__)
            yield gauge('collector_up','Aggregate SQLite snapshot succeeded at latest attempt',[((),int(self.success==self.checked))])
            yield gauge('collector_last_success_timestamp_seconds','Last successful aggregate snapshot',[((),self.success)])
            yield from self.cached

    def inference(self, conn, now):
        counts = {r['state']:r['n'] for r in conn.execute('SELECT state,COUNT(*) n FROM jobs GROUP BY state')}
        queued = conn.execute("SELECT MIN(created) FROM jobs WHERE state='queued'").fetchone()[0]
        return [gauge('inference_jobs','Persisted inference jobs',[((s,),counts.get(s,0)) for s in STATES],['state']),
                gauge('inference_oldest_queued_age_seconds','Oldest queued inference age',[((),max(0,now-queued) if queued else 0)])]

    def application(self, conn, now):
        result = []
        jobs = list(conn.execute('SELECT kind,state,stage,total,completed,failed,created_at,json_extract(payload,\'$.retry_at\') retry_at FROM jobs'))
        counts = {}; remaining = {}; waiting = {}; oldest = {}; storage_jobs={};storage_remaining={};storage_retry={};provider_wait_jobs={}
        for r in jobs:
            kind = bounded(r['kind'],KINDS); state = bounded(r['state'],STATES)
            counts[kind,state] = counts.get((kind,state),0)+1
            if state in {'queued','running','paused','waiting_authorization'}:
                remaining[kind] = remaining.get(kind,0)+max(0,r['total']-r['completed']-r['failed'])
            if state in {'queued','running','paused'} and r['stage']=='waiting_provider':
                waiting[kind] = max(waiting.get(kind,0),max(0,(r['retry_at'] or 0)-now))
                provider_wait_jobs[kind]=provider_wait_jobs.get(kind,0)+1
            if state in {'queued','running','paused'} and r['stage']=='waiting_storage':
                storage_jobs[kind]=storage_jobs.get(kind,0)+1
                storage_remaining[kind]=storage_remaining.get(kind,0)+max(0,r['total']-r['completed']-r['failed'])
                storage_retry[kind]=max(storage_retry.get(kind,0),max(0,(r['retry_at'] or 0)-now))
            if state=='queued':
                from datetime import datetime
                age = max(0,now-datetime.fromisoformat(r['created_at']).timestamp()); oldest[kind]=max(oldest.get(kind,0),age)
        result.append(gauge('jobs','Persisted jobs by kind/state',[((k,s),counts.get((k,s),0)) for k in (*KINDS,'other') for s in (*STATES,'other')],['kind','state']))
        for name,help,values in [('queue_items_remaining','Unfinished items in open jobs, including authorization waits',remaining),('provider_retry_wait_seconds','Provider retry wait remaining',waiting),('oldest_queued_age_seconds','Age of oldest queued job, including provider waits',oldest)]:
            result.append(gauge(name,help,[((k,),values.get(k,0)) for k in KINDS],['kind']))
        for name,help,values in [('storage_wait_jobs','Jobs waiting for safe storage reservation',storage_jobs),
                                 ('storage_wait_items_remaining','Unfinished items in storage-wait jobs',storage_remaining),
                                 ('storage_retry_wait_seconds','Delay before retrying storage reservation',storage_retry),
                                 ('provider_retry_jobs','Jobs waiting for a provider retry',provider_wait_jobs)]:
            result.append(gauge(name,help,[((k,),values.get(k,0)) for k in KINDS],['kind']))
        notification_states=('pending','in_flight','delivered','dead')
        outbox={r['state']:r['n'] for r in conn.execute('SELECT state,COUNT(*) n FROM webhook_outbox GROUP BY state')}
        reserved=conn.execute('SELECT COUNT(*) FROM webhook_reservations').fetchone()[0]
        webhook_row=conn.execute("SELECT value FROM settings WHERE key='webhook_runtime'").fetchone()
        webhook=json.loads(webhook_row[0]) if webhook_row else {}
        paused_row=conn.execute("SELECT value FROM settings WHERE key='webhook_paused'").fetchone()
        recovery_row=conn.execute("SELECT value FROM settings WHERE key='recovery_required'").fetchone()
        queue=conn.execute("SELECT MIN(julianday(created_at)) oldest FROM webhook_outbox WHERE state IN ('pending','in_flight')").fetchone()
        retry=conn.execute("SELECT MIN(next_attempt) due FROM webhook_outbox WHERE state='pending'").fetchone()
        other=conn.execute("SELECT COUNT(*) FROM webhook_outbox WHERE state IN ('pending','in_flight') AND destination_id<>?",(webhook.get('destination_id',''),)).fetchone()[0]
        result.append(gauge('webhook_outbox','Durable notification records; terminal counts may decrease after retention',[((state,),outbox.get(state,0)) for state in notification_states],['state']))
        for name,help,value in [
            ('webhook_reserved_jobs','Terminal notification slots reserved for accepted music jobs',reserved),
            ('webhook_queue_capacity','Configured durable notification capacity',webhook.get('max_pending',self.settings.webhook_max_pending)),
            ('webhook_queue_slots_used','Pending/in-flight notifications plus reserved terminal slots',reserved+outbox.get('pending',0)+outbox.get('in_flight',0)),
            ('webhook_delivery_enabled','Operator configuration enables delivery',int(bool(webhook.get('enabled')))),
            ('webhook_delivery_paused','Owner has paused notification delivery',int(bool(paused_row and paused_row[0]=='true'))),
            ('webhook_recovery_suspended','Restore recovery mode suspends notifications',int(bool(recovery_row and recovery_row[0]=='true'))),
            ('webhook_other_destination_pending','Pending notifications bound to a previous destination',other),
            ('webhook_oldest_pending_age_seconds','Age of oldest undelivered notification',max(0,now-(queue['oldest']-2440587.5)*86400) if queue['oldest'] else 0),
            ('webhook_retry_wait_seconds','Delay before earliest pending notification retry',max(0,(retry['due'] or 0)-now))]:
            result.append(gauge(name,help,[((),value)]))
        item_counts={}
        item_states=('running','succeeded','deferred','failed','needs_review','conflict','interrupted')
        for r in conn.execute('SELECT j.kind,i.state,COUNT(*) n FROM job_items i JOIN jobs j ON j.id=i.job_id GROUP BY j.kind,i.state'):
            key=(bounded(r['kind'],KINDS),bounded(r['state'],item_states));item_counts[key]=item_counts.get(key,0)+r['n']
        result.append(gauge('job_items','Persisted latest item state, not a monotonic counter',[((k,s),item_counts.get((k,s),0)) for k in KINDS for s in item_states],['kind','state']))
        heartbeat_row=conn.execute("SELECT value FROM settings WHERE key='worker_heartbeat'").fetchone()
        heartbeat=json.loads(heartbeat_row[0]) if heartbeat_row else {}; age=max(0,now-heartbeat.get('at',0))
        result += [gauge('worker_heartbeat_age_seconds','Worker heartbeat age',[((),age)]),gauge('worker_online','Fresh active worker heartbeat',[((),int(age<30 and heartbeat.get('state')!='stopped'))])]
        for key in ('file_workers','io_threads'): result.append(gauge('worker_'+key,'Configured worker concurrency',[((),heartbeat.get(key,0))]))
        used = {r['provider']:r['n'] for r in conn.execute('SELECT provider,COUNT(*) n FROM provider_calls WHERE at>? GROUP BY provider',(now-86400,))}
        cooldown = {}; cache = {}
        for r in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'provider_breaker:%'"):
            cooldown[r['key'].split(':',1)[1]]=max(0,json.loads(r['value']).get('until',0)-now)
        for r in conn.execute('SELECT provider,COUNT(*) n,COALESCE(SUM(length(payload)),0) bytes FROM provider_cache WHERE expires>? GROUP BY provider',(now,)):
            cache[r['provider']]=(r['n'],r['bytes'])
        for name,help,fn in [('provider_requests_24h','Requests reserved in rolling 24h; gauge',lambda p:used.get(p,0)),('provider_budget_remaining','Remaining rolling daily request budget',lambda p:max(0,self.settings.provider_daily_limit-used.get(p,0))),('provider_cooldown_seconds','Provider cooldown remaining',lambda p:cooldown.get(p,0)),('provider_cache_entries','Non-expired cache entries',lambda p:cache.get(p,(0,0))[0]),('provider_cache_bytes','Non-expired cache payload bytes',lambda p:cache.get(p,(0,0))[1])]:
            result.append(gauge(name,help,[((p,),fn(p)) for p in PROVIDERS],['provider']))
        recognition=conn.execute("SELECT COUNT(*) identities,COALESCE(SUM(json_extract(value,'$.calls')),0) spent,SUM(CASE WHEN json_extract(value,'$.calls')>=json_extract(value,'$.limit') THEN 1 ELSE 0 END) exhausted FROM settings WHERE key LIKE 'recognition_usage:%'").fetchone()
        result.append(gauge('recognition_budget_identities','Persistent audio identity quotas; a new batch does not reset them',[(('open',),recognition['identities']-(recognition['exhausted'] or 0)),(('exhausted',),recognition['exhausted'] or 0)],['state']))
        result.append(gauge('recognition_budget_spent_calls','Fingerprint reservations shared across providers and batches',[((),recognition['spent'])]))
        formats=('mp3','flac','m4a','mp4','ogg','opus','wav','aiff','aac','webm','mka','wma','ape','wv','cue','dsf','dff','ac3','unknown')
        format_counts={}; statuses={}; youtube={}
        for r in conn.execute("SELECT json_extract(metadata,'$.format') format,status,json_extract(metadata,'$.youtube.state') youtube,COUNT(*) n FROM tracks WHERE is_current=1 GROUP BY format,status,youtube"):
            f=bounded(r['format'],formats);format_counts[f]=format_counts.get(f,0)+r['n']
            s=bounded(r['status'],('indexed','ready','published','needs_review','missing'));statuses[s]=statuses.get(s,0)+r['n']
            s=bounded(r['youtube'] or 'unknown',('unknown','recorded','owner_confirmed','candidates','conflict','unlinked'));youtube[s]=youtube.get(s,0)+r['n']
        result += [gauge('collection_files','Current catalogue entries',[((),sum(statuses.values()))]),gauge('collection_formats','Current formats',[((k,),v) for k,v in format_counts.items()],['format']),gauge('collection_states','Current catalogue states',[((k,),v) for k,v in statuses.items()],['state']),gauge('youtube_links','Current YouTube provenance states',[((k,),youtube.get(k,0)) for k in ('unknown','recorded','owner_confirmed','candidates','conflict','unlinked')],['state'])]
        quality=conn.execute("""SELECT SUM(CASE WHEN json_extract(metadata,'$.has_art') THEN 1 ELSE 0 END) cover,
          SUM(CASE WHEN length(COALESCE(json_extract(metadata,'$.lrc'),''))>0 THEN 1 ELSE 0 END) lrc,
          SUM(CASE WHEN json_type(metadata,'$.input_error')='object' THEN 1 ELSE 0 END) input_error,
          SUM(CASE WHEN json_extract(metadata,'$.write_supported') AND NOT COALESCE(json_extract(metadata,'$.cue_album_image'),0) THEN 1 ELSE 0 END) writable
          FROM tracks WHERE is_current=1""").fetchone()
        result.append(gauge('collection_features','Current file features',[((key,),quality[key] or 0) for key in quality.keys()],['feature']))
        sources={}
        for r in conn.execute("""SELECT json_extract(s.value,'$.provider') provider,json_extract(s.value,'$.state') state,COUNT(*) n
          FROM analyses a JOIN tracks t ON t.id=a.track_id AND t.sha256=a.input_sha256 AND t.is_current=1,
          json_each(a.payload,'$.sources') s GROUP BY provider,state"""):
            p=bounded(r['provider'],PROVIDERS);s=bounded(r['state'],('fingerprint_match','no_match','candidates','provider_unavailable','provider_cooldown','rate_limited','budget_exhausted','provider_disabled','recognition_attempt_budget_exhausted','recognition_identity_required'))
            sources[p,s]=sources.get((p,s),0)+r['n']
        result.append(gauge('analysis_sources','Current revision analysis outcomes',[((p,s),n) for (p,s),n in sources.items()],['provider','state']))
        return result
