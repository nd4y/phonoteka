"""Owner-issued, expiring API credentials. Only hashes are stored."""
import json
import re
import secrets
import time
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from . import db, security

SCOPES={'read','jobs:analysis','jobs:write'}


class TokenInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    scopes:list[str]=Field(min_length=1,max_length=3)
    days:int=Field(default=30,ge=1,le=365)


def check_job(active,kind):
    if not active.get('api_token_id'):return
    required='jobs:write' if kind in {'apply','rollback','publish','extract_cue','normalize_audio'} else 'jobs:analysis'
    if required not in active['scopes']:raise security.error(403,'scope_denied','Токен не разрешает этот вид задания')


def bearer(request,settings):
    header=request.headers.get('authorization','')
    if not header.startswith('Bearer ') or not 32<len(header)<256:
        raise security.error(401,'invalid_api_token','Недействительный API-токен')
    row=db.fetchone(settings.db_path,'SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0 AND expires>?',
                    (security.token_hash(header[7:]),time.time()))
    if not row or row['auth_epoch']!=int(db.setting(settings.db_path,'auth_epoch','0')):
        raise security.error(401,'invalid_api_token','API-токен истек или отозван')
    scopes=json.loads(row['scopes']);path=request.url.path.replace('/api/v1/','/api/',1)
    allowed=False
    if request.method=='GET':
        allowed='read' in scopes and (path in {'/api/summary','/api/providers','/api/events','/api/openapi.json'} or path.startswith(('/api/tracks','/api/jobs','/api/reports','/api/exports')))
    elif request.method=='POST':
        if path=='/api/jobs' or re.fullmatch(r'/api/jobs/[^/]+/(cancel|pause|resume)',path):allowed=bool(set(scopes)&{'jobs:analysis','jobs:write'})
        elif re.fullmatch(r'/api/tracks/[^/]+/(proposal|analysis/select)',path):allowed='jobs:write' in scopes
        elif path=='/api/deliveries':allowed='jobs:write' in scopes
    if not allowed:raise security.error(403,'scope_denied','Недостаточно прав API-токена')
    if request.headers.get('origin') not in (None,settings.app_url) or request.headers.get('sec-fetch-site')=='cross-site':
        raise security.error(403,'origin_denied','Межсайтовый запрос не разрешен')
    with db.transaction(settings.db_path) as conn:conn.execute('UPDATE api_tokens SET last_used=? WHERE id=?',(time.time(),row['id']))
    return {'auth_epoch':row['auth_epoch'],'api_token_id':row['id'],'scopes':scopes}


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
            conn.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated_at=? WHERE json_extract(payload,'$.auth_token_id')=? AND state IN ('queued','running')",(db.utcnow(),token_id))
            db.event(conn,'api_token_revoked',{'id':token_id})
        return {'revoked':True}
