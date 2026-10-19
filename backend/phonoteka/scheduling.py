"""Durable single-owner schedules; resolve each stage after the previous scan."""
import json
import time
import uuid

from fastapi import Request
from pydantic import BaseModel,ConfigDict,Field
from . import db,security,input_readiness

KINDS={'scan','analyze','recognize','replaygain'}


class ScheduleInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:bool=False
    interval_minutes:int=Field(default=1440,ge=30,le=43200)
    steps:list[str]=Field(default=['scan','recognize','analyze'],min_length=1,max_length=4)


def install(app,settings):
    @app.get('/api/schedule')
    def get_schedule(request:Request):
        security.require_session(request,settings,owner_only=True)
        return json.loads(db.setting(settings.db_path,'schedule','{}'))

    @app.put('/api/schedule')
    def set_schedule(request:Request,body:ScheduleInput):
        active=security.require_session(request,settings,True,owner_only=True)
        if set(body.steps)-KINDS or len(set(body.steps))!=len(body.steps):raise security.error(422,'invalid_steps','Неверные этапы расписания')
        if body.enabled and (('recognize' in body.steps and not settings.external_fingerprints) or ('analyze' in body.steps and not settings.external_metadata)):
            raise security.error(409,'provider_disabled','Источник для расписания выключен')
        state={**body.model_dump(),'auth_epoch':active['auth_epoch'],'next_run':time.time()+body.interval_minutes*60,'remaining':[],'last_run':None}
        with db.transaction(settings.db_path) as conn:
            conn.execute("INSERT INTO settings VALUES('schedule',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(db.encode(state),))
            if not body.enabled:
                jobs=conn.execute("SELECT id,state,completed FROM jobs WHERE json_extract(payload,'$.trigger')='schedule' AND state IN ('queued','running','paused')").fetchall()
                for job in jobs:
                    terminal=job['state'] in {'queued','paused'}
                    outcome=('cancelled_partial' if job['completed'] else 'cancelled') if terminal else job['state']
                    conn.execute('UPDATE jobs SET cancel_requested=1,state=?,stage=CASE WHEN ? THEN ? ELSE stage END,error=CASE WHEN ? THEN ? ELSE error END,updated_at=? WHERE id=?',
                                 (outcome,terminal,outcome,terminal,'schedule_disabled',db.utcnow(),job['id']))
                    db.event(conn,'job_cancel_requested',{'job_id':job['id'],'code':'schedule_disabled'})
            db.event(conn,'schedule_updated',{'enabled':body.enabled,'steps':body.steps,'interval_minutes':body.interval_minutes})
        return state


def tick(settings,now=None):
    if db.setting(settings.db_path,'recovery_required','false')=='true':return
    now=time.time() if now is None else now
    with db.transaction(settings.db_path) as conn:
        row=conn.execute("SELECT value FROM settings WHERE key='schedule'").fetchone()
        state=json.loads(row['value']) if row else {}
        if not state.get('enabled'):return
        epoch=int(conn.execute("SELECT value FROM settings WHERE key='auth_epoch'").fetchone()[0])
        if state.get('auth_epoch')!=epoch:
            state.update(enabled=False,remaining=[],disabled_reason='authorization_changed')
            conn.execute("UPDATE settings SET value=? WHERE key='schedule'",(db.encode(state),));return
        if conn.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():return
        if not state.get('remaining'):
            if state.get('next_run',now)>now:return
            state['remaining']=list(state['steps']);state['last_run']=now
        kind=state['remaining'].pop(0);items=[];waiting_inputs=0
        enabled=kind not in {'analyze','recognize'} or (settings.external_metadata if kind=='analyze' else settings.external_fingerprints)
        if kind!='scan' and enabled:
            sources={}
            for row in conn.execute("SELECT a.track_id,json_extract(s.value,'$.provider') AS provider,json_extract(s.value,'$.state') AS state FROM analyses a JOIN tracks t ON t.id=a.track_id AND t.sha256=a.input_sha256 AND t.is_current=1,json_each(a.payload,'$.sources') s"):
                sources.setdefault(row['track_id'],{})[row['provider']]=row['state']
            wanted=['shazam'] if kind=='recognize' else ['local_replaygain'] if kind=='replaygain' else ['musicbrainz','lrclib']
            for row in conn.execute("SELECT id,relative_path,sha256,metadata FROM tracks WHERE is_current=1 AND status NOT IN ('error','missing') AND json_extract(metadata,'$.format')<>'cue' AND COALESCE(json_extract(metadata,'$.cue_album_image'),0)=0 LIMIT 10000"):
                if any(sources.get(row['id'],{}).get(p) not in {'candidates','candidate','fingerprint_match','no_match'} for p in wanted):
                    if input_readiness.describe(json.loads(row['metadata']),grace_seconds=settings.input_grace_seconds,now=now)['state'] != 'ready':
                        waiting_inputs+=1
                        continue
                    items.append({'track_id':row['id'],'relative_path':row['relative_path'],'sha256':row['sha256']})
        state['waiting_inputs']=waiting_inputs
        if waiting_inputs:state['waiting_reason']='input_stabilizing'
        elif state.get('waiting_reason')=='input_stabilizing':state.pop('waiting_reason',None)
        if enabled and (items or kind=='scan'):
            from .webhooks import can_accept
            if not can_accept(conn):
                state['remaining'].insert(0,kind)
                state['waiting_reason']='webhook_queue_full'
                conn.execute("UPDATE settings SET value=? WHERE key='schedule'",(db.encode(state),))
                return
            if not waiting_inputs:state.pop('waiting_reason',None)
            identity=str(uuid.uuid4());stamp=db.utcnow()
            payload={'items':items,'params':{},'trigger':'schedule'}
            conn.execute("INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at,auth_epoch) VALUES(?,?,'queued','pending',?,?,?,?,?)",(identity,kind,db.encode(payload),len(items),stamp,stamp,epoch))
            db.event(conn,'job_created',{'job_id':identity,'kind':kind,'trigger':'schedule'})
            state['last_job_id']=identity
        if not state['remaining']:state['next_run']=now+state['interval_minutes']*60
        conn.execute("UPDATE settings SET value=? WHERE key='schedule'",(db.encode(state),))
