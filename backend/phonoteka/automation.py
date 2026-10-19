"""Owner-issued, expiring API credentials. Only hashes are stored."""
import json
import re
import secrets
import time
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from . import db, security

CANONICAL_SCOPES={'library:read','jobs:create','files:write','publish:write'}
LEGACY_SCOPES={'read','jobs:analysis','jobs:write'}
SCOPES=CANONICAL_SCOPES|LEGACY_SCOPES
FILE_JOBS={'apply','rollback','extract_cue','normalize_audio'}
PUBLICATION_JOBS={'publish','rollback_ready','publish_sidecars','export_ready'}
ANALYSIS_JOBS={'scan','audit','analyze','recognize','transcribe','align','replaygain','replaygain_album','youtube','release_audit'}
# These are compatibility grants, not broad aliases for jobs:create. In
# particular old jobs:write never permitted analysis, and old jobs:analysis
# permitted export_ready but no other publication or delivery operation.
LEGACY_WRITE_JOBS=FILE_JOBS|{'publish','rollback_ready','publish_sidecars'}
LEGACY_ANALYSIS_JOBS=ANALYSIS_JOBS|{'export_ready'}


class TokenInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    scopes:list[str]=Field(min_length=1,max_length=7)
    days:int=Field(default=30,ge=1,le=365)


def scope_set(value):
    """Fail closed for malformed stored/snapshotted scopes."""
    if isinstance(value,str):
        try:value=json.loads(value)
        except (TypeError,ValueError):return frozenset()
    if not isinstance(value,(list,tuple,set,frozenset)) or not value or len(value)>7:
        return frozenset()
    if any(not isinstance(item,str) or item not in SCOPES for item in value):
        return frozenset()
    return frozenset(value)


def has_scope(scopes,required):
    scopes=scope_set(scopes)
    aliases={'library:read':{'read'},'files:write':{'jobs:write'},'publish:write':{'jobs:write'}}
    return required in CANONICAL_SCOPES and bool(scopes&({required}|aliases.get(required,set())))


def job_scope_allowed(scopes,kind):
    scopes=scope_set(scopes)
    if kind not in ANALYSIS_JOBS|FILE_JOBS|PUBLICATION_JOBS:return False
    if 'jobs:create' in scopes:
        if kind in FILE_JOBS and has_scope(scopes,'files:write'):return True
        if kind in PUBLICATION_JOBS and has_scope(scopes,'publish:write'):return True
        if kind in ANALYSIS_JOBS:return True
    return bool('jobs:analysis' in scopes and kind in LEGACY_ANALYSIS_JOBS
                or 'jobs:write' in scopes and kind in LEGACY_WRITE_JOBS)


def token_grant_allowed(settings,*,token_id,auth_epoch,kind,granted_scopes=None):
    """Recheck accepted and live powers before an effect, without job payloads.

    Old jobs have no scope snapshot. Only their legacy names may authorize
    those jobs; adding canonical powers later cannot broaden a past grant.
    The worker supplies the small JSON projection from job_control.
    """
    if not token_id:return True
    epoch=int(db.setting(settings.db_path,'auth_epoch','0'))
    token=db.fetchone(settings.db_path,'SELECT scopes,revoked,expires,auth_epoch FROM api_tokens WHERE id=?',(token_id,))
    if not token or token['revoked'] or token['expires']<=time.time() or token['auth_epoch']!=epoch or auth_epoch!=epoch:
        return False
    current=scope_set(token['scopes'])
    accepted=scope_set(granted_scopes) if granted_scopes is not None else current&LEGACY_SCOPES
    return job_scope_allowed(current,kind) and job_scope_allowed(accepted,kind)


def check_scope(active,required,message='Недостаточно прав API-токена'):
    if active.get('api_token_id') and not has_scope(active.get('scopes'),required):
        raise security.error(403,'scope_denied',message)


def check_job(active,kind):
    if active.get('api_token_id') and not job_scope_allowed(active.get('scopes'),kind):
        raise security.error(403,'scope_denied','Токен не разрешает этот вид задания')


def bearer(request,settings):
    header=request.headers.get('authorization','')
    if not header.startswith('Bearer ') or not 32<len(header)<256:
        raise security.error(401,'invalid_api_token','Недействительный API-токен')
    row=db.fetchone(settings.db_path,'SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0 AND expires>?',
                    (security.token_hash(header[7:]),time.time()))
    if not row or row['auth_epoch']!=int(db.setting(settings.db_path,'auth_epoch','0')):
        raise security.error(401,'invalid_api_token','API-токен истек или отозван')
    scopes=scope_set(row['scopes']);path=request.url.path.replace('/api/v1/','/api/',1)
    allowed=False
    if request.method=='GET':
        allowed=has_scope(scopes,'library:read') and (path in {'/api/summary','/api/providers','/api/events','/api/openapi.json'} or path.startswith(('/api/tracks','/api/jobs','/api/reports','/api/exports','/api/release-reviews','/api/publications','/api/album-gain-groups')))
    elif request.method=='POST':
        if path=='/api/jobs' or re.fullmatch(r'/api/jobs/[^/]+/(cancel|pause|resume)',path):allowed=bool(scopes&{'jobs:create','jobs:analysis','jobs:write'})
        elif re.fullmatch(r'/api/jobs/[^/]+/retry-storage',path):allowed='jobs:create' in scopes
        elif re.fullmatch(r'/api/tracks/[^/]+/(proposal|analysis/select)',path):allowed=has_scope(scopes,'files:write')
        elif path in {'/api/deliveries','/api/publications/plan','/api/export-leases'} or re.fullmatch(r'/api/export-leases/[^/]+/renew',path):allowed=has_scope(scopes,'publish:write')
        elif re.fullmatch(r'/api/proposals/[^/]+/apply',path):allowed=job_scope_allowed(scopes,'apply')
        elif re.fullmatch(r'/api/publications/[^/]+/commit',path):allowed=job_scope_allowed(scopes,'publish')
        elif re.fullmatch(r'/api/revisions/[^/]+/rollback',path):allowed=job_scope_allowed(scopes,'rollback') or job_scope_allowed(scopes,'rollback_ready')
    elif request.method=='PATCH' and re.fullmatch(r'/api/proposals/[^/]+',path):
        allowed=has_scope(scopes,'files:write')
    elif request.method=='DELETE' and re.fullmatch(r'/api/export-leases/[^/]+',path):
        allowed=has_scope(scopes,'publish:write')
    if not allowed:raise security.error(403,'scope_denied','Недостаточно прав API-токена')
    if request.headers.get('origin') not in (None,settings.app_url) or request.headers.get('sec-fetch-site')=='cross-site':
        raise security.error(403,'origin_denied','Межсайтовый запрос не разрешен')
    with db.transaction(settings.db_path) as conn:conn.execute('UPDATE api_tokens SET last_used=? WHERE id=?',(time.time(),row['id']))
    return {'auth_epoch':row['auth_epoch'],'api_token_id':row['id'],'scopes':sorted(scopes)}


def install(app,settings):
    @app.get('/api/tokens')
    def tokens(request:Request):
        security.require_session(request,settings,owner_only=True)
        rows=db.fetchall(settings.db_path,'SELECT id,name,scopes,created,expires,last_used,revoked FROM api_tokens ORDER BY created DESC')
        return {'items':[{**r,'scopes':json.loads(r['scopes'])} for r in rows]}

    @app.post('/api/tokens',status_code=201)
    def create(request:Request,body:TokenInput):
        active=security.require_session(request,settings,True,owner_only=True)
        if set(body.scopes)-SCOPES:raise security.error(422,'invalid_scopes','Неизвестная область доступа')
        identity=str(uuid.uuid4());raw='phk_'+secrets.token_urlsafe(48);now=time.time()
        with db.transaction(settings.db_path) as conn:
            if conn.execute('SELECT COUNT(*) FROM api_tokens WHERE revoked=0 AND expires>?',(now,)).fetchone()[0]>=20:
                raise security.error(409,'token_limit','Не более 20 активных токенов')
            conn.execute('INSERT INTO api_tokens VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,security.token_hash(raw),body.name,db.encode(sorted(set(body.scopes))),now,now+body.days*86400,None,0,active['auth_epoch'],db.utcnow()))
            db.event(conn,'api_token_created',{'id':identity,'name':body.name,'scopes':body.scopes})
        return {'id':identity,'token':raw,'expires':now+body.days*86400,'scopes':body.scopes}

    @app.delete('/api/tokens/{token_id}')
    def revoke(request:Request,token_id:str):
        security.require_session(request,settings,True,owner_only=True)
        with db.transaction(settings.db_path) as conn:
            if not conn.execute('SELECT id FROM api_tokens WHERE id=?',(token_id,)).fetchone():raise security.error(404,'token_not_found','Токен не найден')
            conn.execute('UPDATE api_tokens SET revoked=1 WHERE id=?',(token_id,))
            jobs=conn.execute("SELECT id,state,completed FROM jobs WHERE json_extract(payload,'$.auth_token_id')=? AND state IN ('queued','running','paused')",(token_id,)).fetchall()
            for job in jobs:
                terminal=job['state'] in {'queued','paused'}
                state=('cancelled_partial' if job['completed'] else 'cancelled') if terminal else job['state']
                conn.execute('UPDATE jobs SET cancel_requested=1,state=?,stage=CASE WHEN ? THEN ? ELSE stage END,error=CASE WHEN ? THEN ? ELSE error END,updated_at=? WHERE id=?',
                             (state,terminal,state,terminal,'authorization_revoked',db.utcnow(),job['id']))
                # The state transition and durable result notification consume
                # the accepted job's outbox reservation in this transaction.
                db.event(conn,'job_cancel_requested',{'job_id':job['id'],'code':'authorization_revoked'})
            db.event(conn,'api_token_revoked',{'id':token_id})
        return {'revoked':True}
