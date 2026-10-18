"""Optional signed terminal-job notifications, with transactional durable outbox.

No endpoint is enabled by default. Only an operator-supplied HTTPS destination
can be used. Notification retries never submit a music job or repeat its effects.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from fastapi import Query, Request
from fastapi.responses import JSONResponse
import httpx

from . import db, security
from .telemetry import log

RUNTIME_KEY = 'webhook_runtime'
PAUSE_KEY = 'webhook_paused'
TERMINAL = {'succeeded','failed','completed_with_errors','cancelled','cancelled_partial','interrupted','conflict','needs_review'}
KINDS = {'scan','audit','apply','rollback','rollback_ready','publish','analyze','recognize','transcribe','align','extract_cue','replaygain','replaygain_album','export_ready','normalize_audio','youtube','release_audit'}
SAFE_CODES = {'partial_scan','conflict','revision_conflict','needs_review','audit_failed','cancelled','internal_error','storage_error','storage_unavailable','provider_policy_denied','provider_policy_expired','authorization_revoked','schedule_disabled','inference_timeout','decode_failed','unsupported_format','field_locked','field_lock_stale','duplicate_decision_stale','preferred_copy_selected'}
MAX_ATTEMPTS = 8
REPLAY_WINDOW_SECONDS = 300


class OutboxFull(Exception):
    pass


def _get(conn, key, default='{}'):
    row=conn.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return row['value'] if row else default


def runtime(conn):
    return json.loads(_get(conn,RUNTIME_KEY))


def can_accept(conn):
    config=runtime(conn)
    if not config.get('enabled'):return True
    occupied=conn.execute("SELECT COUNT(*) FROM webhook_outbox WHERE state IN ('pending','in_flight')").fetchone()[0]
    occupied+=conn.execute('SELECT COUNT(*) FROM webhook_reservations').fetchone()[0]
    return occupied<config['max_pending']


def enqueue(conn,event_id,kind,payload,created_at):
    config=runtime(conn)
    identity=payload.get('job_id')
    if not isinstance(identity,str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,80}',identity):return
    if kind=='job_created':
        if not config.get('enabled'):return
        if conn.execute('SELECT 1 FROM webhook_reservations WHERE job_id=?',(identity,)).fetchone():return
        if not can_accept(conn):raise OutboxFull()
        conn.execute('INSERT OR IGNORE INTO webhook_reservations VALUES(?,?)',(identity,config['destination_id']))
        return
    if kind not in {'job_finished','job_cancel_requested'}:return
    job=conn.execute('SELECT id,kind,state,total,completed,failed,error FROM jobs WHERE id=?',(identity,)).fetchone()
    if not job or job['state'] not in TERMINAL:return
    reservation=conn.execute('SELECT destination_id FROM webhook_reservations WHERE job_id=?',(identity,)).fetchone()
    if not reservation:return
    instance=_get(conn,'webhook_event_epoch',_get(conn,'instance_uuid','unknown'))
    stable_id=f'{instance}:{event_id}'
    safe_job={name:job[name] for name in ('id','state','total','completed','failed')}
    safe_job['kind']=job['kind'] if job['kind'] in KINDS else 'other'
    if job['error']:safe_job['code']=job['error'] if job['error'] in SAFE_CODES else 'processing_error'
    document={'id':stable_id,'event_id':event_id,'type':'job_finished','created_at':created_at,'job':safe_job}
    conn.execute("INSERT OR IGNORE INTO webhook_outbox(id,event_id,destination_id,job_id,payload,state,attempts,next_attempt,created_at,updated_at) VALUES(?,?,?,?,?,'pending',0,0,?,?)",(stable_id,event_id,reservation['destination_id'],identity,db.encode(document),created_at,created_at))
    conn.execute('DELETE FROM webhook_reservations WHERE job_id=?',(identity,))


def endpoint(settings):
    parsed=urlsplit(settings.webhook_url)
    host=(parsed.hostname or '').lower()
    if (parsed.scheme!='https' or not host or host not in settings.webhook_allowed_hosts or
        parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None,443) or
        len(settings.webhook_url)>2000):raise ValueError('invalid_webhook_destination')
    return parsed,host


def secret(settings):
    path=settings.webhook_secret_file
    if not path or not path.is_file() or path.is_symlink() or path.stat().st_size>4096:
        raise ValueError('webhook_secret_unavailable')
    value=path.read_bytes().rstrip(b'\r\n')
    if not 32<=len(value)<=4096:raise ValueError('webhook_secret_invalid')
    return value


def configure(settings):
    config={'enabled':False,'configured':False,'max_pending':settings.webhook_max_pending,'destination_id':'','error':None}
    if settings.webhook_enabled:
        try:
            _parsed,host=endpoint(settings);secret(settings)
            config.update(enabled=True,configured=True,host=host,destination_id=hashlib.sha256(settings.webhook_url.encode()).hexdigest())
        except (ValueError,OSError):config['error']='invalid_operator_configuration'
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('webhook_event_epoch',str(uuid.uuid4())))
        if config['enabled']:
            pending=conn.execute("SELECT COUNT(*) FROM webhook_outbox WHERE state IN ('pending','in_flight')").fetchone()[0]
            active=conn.execute("SELECT id FROM jobs WHERE state IN ('queued','running','paused') AND id NOT IN (SELECT job_id FROM webhook_reservations)").fetchall()
            reserved=conn.execute('SELECT COUNT(*) FROM webhook_reservations').fetchone()[0]
            if pending+reserved+len(active)>settings.webhook_max_pending:
                config.update(enabled=False,error='existing_queue_exceeds_capacity')
            else:
                for row in active:conn.execute('INSERT OR IGNORE INTO webhook_reservations VALUES(?,?)',(row['id'],config['destination_id']))
        conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(RUNTIME_KEY,db.encode(config)))
    return config


def signing_bytes(body,timestamp,event_id):
    return str(timestamp).encode()+b'.'+event_id.encode()+b'.'+body


def signature(key,body,timestamp,event_id):
    return 'sha256='+hmac.new(key,signing_bytes(body,timestamp,event_id),hashlib.sha256).hexdigest()


def verify_signature(key,body,timestamp,event_id,provided,now=None):
    try:
        stamp=int(timestamp)
        if abs((time.time() if now is None else now)-stamp)>REPLAY_WINDOW_SECONDS:return False
        return hmac.compare_digest(signature(key,body,stamp,event_id),provided)
    except (TypeError,ValueError,AttributeError):return False


def resolve(host,allow_private=False):
    addresses=sorted({entry[4][0] for entry in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)})
    if not addresses:raise OSError('dns_failed')
    for value in addresses:
        ip=ipaddress.ip_address(value)
        if isinstance(ip,ipaddress.IPv6Address) and ip.ipv4_mapped:ip=ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError('unsafe_webhook_destination')
        if not ip.is_global and not (allow_private and ip.is_private):raise ValueError('unsafe_webhook_destination')
    return addresses[0]


def claim(settings,now=None):
    now=time.time() if now is None else now
    with db.transaction(settings.db_path) as conn:
        config=runtime(conn)
        _prune_terminal(conn,now)
        if not config.get('enabled') or _get(conn,PAUSE_KEY,'false')=='true' or _get(conn,'recovery_required','false')=='true':return None
        conn.execute("UPDATE webhook_outbox SET state=CASE WHEN attempts>=? THEN 'dead' ELSE 'pending' END,last_code='delivery_unknown_after_lease',lease_token=NULL,leased_until=NULL WHERE state='in_flight' AND leased_until<=?",(MAX_ATTEMPTS,now))
        row=conn.execute("SELECT * FROM webhook_outbox WHERE state='pending' AND attempts<? AND destination_id=? AND next_attempt<=? ORDER BY event_id LIMIT 1",(MAX_ATTEMPTS,config['destination_id'],now)).fetchone()
        if not row:return None
        token=str(uuid.uuid4())
        conn.execute("UPDATE webhook_outbox SET state='in_flight',attempts=attempts+1,lease_token=?,leased_until=?,updated_at=? WHERE id=? AND state='pending'",(token,now+30,db.utcnow(),row['id']))
        return {**dict(row),'lease_token':token,'attempts':row['attempts']+1}


def complete(settings,row,code,delivered=False,permanent=False,now=None):
    now=time.time() if now is None else now
    state='delivered' if delivered else 'dead' if permanent or row['attempts']>=MAX_ATTEMPTS else 'pending'
    delay=min(3600,5*2**min(row['attempts']-1,10))
    with db.transaction(settings.db_path) as conn:
        updated=conn.execute("UPDATE webhook_outbox SET state=?,next_attempt=?,last_code=?,lease_token=NULL,leased_until=NULL,updated_at=?,delivered_at=? WHERE id=? AND state='in_flight' AND lease_token=?",(state,now+delay,code,db.utcnow(),db.utcnow() if delivered else None,row['id'],row['lease_token'])).rowcount
        if not updated:return 'lease_lost'
        _prune_terminal(conn,now)
    return state


def _prune_terminal(conn,now):
    # Only terminal records are eligible. Pending/reserved jobs are durable.
    conn.execute("DELETE FROM webhook_outbox WHERE state IN ('delivered','dead') AND (julianday(updated_at)<julianday(?,'unixepoch') OR id IN (SELECT id FROM webhook_outbox WHERE state IN ('delivered','dead') ORDER BY updated_at DESC,event_id DESC LIMIT -1 OFFSET 5000))",(now-7*86400,))


class Sender:
    def __init__(self,settings,transport=None,resolver=resolve):
        self.settings=settings;self.transport=transport;self.resolver=resolver

    async def once(self):
        if not self.settings.webhook_enabled:return False
        row=await asyncio.to_thread(claim,self.settings)
        if not row:return False
        code='network_error';delivered=False;permanent=False
        try:
            parsed,host=endpoint(self.settings)
            ip=await asyncio.wait_for(asyncio.to_thread(self.resolver,host,self.settings.webhook_allow_private),timeout=3)
            target=urlunsplit(('https',f'[{ip}]' if ':' in ip else ip,parsed.path or '/','',''))
            body=row['payload'].encode();stamp=int(time.time());key=await asyncio.to_thread(secret,self.settings)
            headers={'Host':f'[{host}]' if ':' in host else host,'Content-Type':'application/json','X-Phonoteka-Event-ID':row['id'],
                     'Idempotency-Key':row['id'],'X-Phonoteka-Timestamp':str(stamp),'X-Phonoteka-Signature':signature(key,body,stamp,row['id'])}
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(timeout=httpx.Timeout(5,connect=3),limits=httpx.Limits(max_connections=1,max_keepalive_connections=0),follow_redirects=False,trust_env=False,transport=self.transport) as client:
                    # Pin the validated address. TLS still verifies the configured
                    # hostname; a second DNS lookup cannot rebind to NAS/metadata.
                    async with client.stream('POST',target,content=body,headers=headers,extensions={'sni_hostname':host}) as response:
                        status=response.status_code
            delivered=200<=status<300
            permanent=300<=status<400 or 400<=status<500 and status not in {408,425,429}
            code='delivered' if delivered else 'redirect_rejected' if 300<=status<400 else f'http_{status}'
        except ValueError:code='unsafe_or_invalid_configuration';permanent=True
        except (OSError,asyncio.TimeoutError,httpx.HTTPError):code='network_error'
        state=await asyncio.to_thread(complete,self.settings,row,code,delivered,permanent)
        log('webhook_delivery',event_id=row['id'],state=state,code=code,attempt=row['attempts'])
        return True

    async def run(self,stop):
        while not stop.is_set():
            try:worked=await self.once()
            except Exception:
                # A notifier exception cannot alter a job or fail API startup.
                log('webhook_sender_error',code='internal_error');worked=False
            try:await asyncio.wait_for(stop.wait(),timeout=.2 if worked else 2)
            except asyncio.TimeoutError:pass


def status(settings):
    with db.connect(settings.db_path) as conn:
        config=runtime(conn)
        counts={row['state']:row['n'] for row in conn.execute('SELECT state,COUNT(*) AS n FROM webhook_outbox GROUP BY state')}
        reservations=conn.execute('SELECT COUNT(*) FROM webhook_reservations').fetchone()[0]
        other=conn.execute("SELECT COUNT(*) FROM webhook_outbox WHERE state IN ('pending','in_flight') AND destination_id<>?",(config.get('destination_id',''),)).fetchone()[0]
        return {'enabled':config.get('enabled',False),'configured':config.get('configured',False),'error':config.get('error'),
                'host':config.get('host'),'paused':_get(conn,PAUSE_KEY,'false')=='true','recovery_suspended':_get(conn,'recovery_required','false')=='true',
                'counts':counts,'reserved_jobs':reservations,'capacity':config.get('max_pending',10000),'other_destination_pending':other,
                'delivery':'at_least_once','events':['job_finished'],'hmac':'sha256','replay_window_seconds':REPLAY_WINDOW_SECONDS,
                'max_attempts':MAX_ATTEMPTS,'terminal_retention_days':7,'pending_auto_delete':False}


def install(app,settings):
    @app.exception_handler(OutboxFull)
    async def full(_request,_error):
        return JSONResponse({'code':'webhook_queue_full','message':'Очередь уведомлений заполнена. Восстановите доставку или отключите webhook в конфигурации; обработка еще не началась.','retryable':True,'retry_after':30},status_code=503,headers={'Retry-After':'30'})

    @app.get('/api/webhooks')
    def get_status(request:Request,page:int=Query(1,ge=1),page_size:int=Query(30,ge=1,le=100)):
        security.require_session(request,settings,owner_only=True)
        rows=db.fetchall(settings.db_path,'SELECT id,event_id,job_id,state,attempts,next_attempt,last_code,created_at,updated_at,delivered_at FROM webhook_outbox ORDER BY event_id DESC LIMIT ? OFFSET ?',(page_size,(page-1)*page_size))
        return {**status(settings),'items':rows,'page':page,'page_size':page_size}

    @app.post('/api/webhooks/pause')
    @app.post('/api/webhooks/resume')
    def pause(request:Request):
        security.require_session(request,settings,True,owner_only=True)
        action=request.url.path.rsplit('/',1)[-1]
        if action=='resume' and not status(settings)['enabled']:raise security.error(409,'webhook_not_configured','Webhook включает оператор в Docker-конфигурации')
        db.set_setting(settings.db_path,PAUSE_KEY,'true' if action=='pause' else 'false')
        return status(settings)

    @app.post('/api/webhooks/{identity}/retry')
    def retry(request:Request,identity:str):
        security.require_session(request,settings,True,owner_only=True)
        with db.transaction(settings.db_path) as conn:
            row=conn.execute('SELECT * FROM webhook_outbox WHERE id=?',(identity,)).fetchone()
            if not row:raise security.error(404,'notification_missing','Уведомление не найдено')
            if row['state']!='dead':raise security.error(409,'notification_not_dead','Повтор доступен для окончательно не доставленного уведомления')
            config=runtime(conn)
            if not config.get('enabled') or config.get('destination_id')!=row['destination_id']:raise security.error(409,'destination_changed','Уведомление связано с другой конфигурацией получателя')
            if not can_accept(conn):raise OutboxFull()
            conn.execute("UPDATE webhook_outbox SET state='pending',attempts=0,next_attempt=0,last_code=NULL,updated_at=? WHERE id=?",(db.utcnow(),identity))
        return {'id':identity,'state':'pending','delivery':'at_least_once'}
