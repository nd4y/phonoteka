"""Durable, content-free HTTP attempt accounting. An unknown result is spent.

The budget and opaque attempt are committed together before any network send.
Neither a timeout nor a process restart can return the reserved call allowance.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
import time
import uuid

from fastapi import Query,Request
from . import db,security
from .telemetry import PROVIDERS

PREFIX='provider_attempt:'
MAX_RECORDS=100000
STALE_SECONDS=180
RETENTION_SECONDS=30*86400
STATES={'reserved','sent','succeeded','no_match','failed','unknown_outcome'}
OPERATIONS={'metadata','lyrics','cover','fingerprint','provenance'}
INPUT=ContextVar('provider_input_revision',default=(None,'unknown'))
REQUEST=ContextVar('provider_request_attempt_state',default=None)
PROCESS_ID=str(uuid.uuid4())


class UsageError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)


@contextmanager
def input_revision(value,kind='file_sha256'):
    valid=isinstance(value,str) and re.fullmatch('[a-fA-F0-9]{64}',value)
    token=INPUT.set((value.lower(),kind) if valid and kind in {'file_sha256','sample_sha256'} else (None,'unknown'))
    try:yield
    finally:INPUT.reset(token)


@contextmanager
def request_scope():
    state={'unknown':False};token=REQUEST.set(state)
    try:yield state
    finally:REQUEST.reset(token)


def unknown_in_request():
    state=REQUEST.get()
    if state is not None:state['unknown']=True


def _maintain(conn,now,force=False):
    row=conn.execute("SELECT value FROM settings WHERE key='provider_attempt_sweep_at'").fetchone()
    if not force and row and now<float(row[0])+60:return
    for row in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'provider_attempt:%'").fetchall():
        value=json.loads(row['value'])
        if value['state'] in {'reserved','sent'} and value['reserved_at']+STALE_SECONDS<now:
            value.update(state='unknown_outcome',code='executor_lost',finished_at=now)
            conn.execute('UPDATE settings SET value=? WHERE key=?',(db.encode(value),row['key']))
        elif value['state'] not in {'reserved','sent'} and value.get('finished_at',now)+RETENTION_SECONDS<now:
            conn.execute('DELETE FROM settings WHERE key=?',(row['key'],))
    conn.execute("INSERT OR REPLACE INTO settings VALUES('provider_attempt_sweep_at',?)",(str(now),))


def recover_stale(settings,now=None):
    with db.transaction(settings.db_path) as conn:_maintain(conn,time.time() if now is None else now,True)


def reserve(settings,provider,operation='metadata',revision=None,kind=None):
    now=time.time();provider=provider if provider in PROVIDERS else 'other';operation=operation if operation in OPERATIONS else 'metadata'
    inherited_revision,inherited_kind=INPUT.get()
    revision=revision or inherited_revision;kind=kind or inherited_kind
    if not isinstance(revision,str) or not re.fullmatch('[a-fA-F0-9]{64}',revision):revision=None;kind='unknown'
    with db.transaction(settings.db_path) as conn:
        _maintain(conn,now)
        conn.execute('DELETE FROM provider_calls WHERE at<?',(now-86400,))
        if conn.execute('SELECT COUNT(*) FROM provider_calls WHERE provider=?',(provider,)).fetchone()[0]>=settings.provider_daily_limit:
            raise UsageError('budget_exhausted')
        if conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'provider_attempt:%'").fetchone()[0]>=MAX_RECORDS:
            # The count cap may shorten terminal history, but never the current
            # budget window and never an unfinished record. Call accounting is
            # retained independently in provider_calls until its 24h cutoff.
            conn.execute("DELETE FROM settings WHERE key IN (SELECT key FROM settings WHERE key LIKE 'provider_attempt:%' AND json_extract(value,'$.state') NOT IN ('reserved','sent') AND json_extract(value,'$.finished_at')<? ORDER BY json_extract(value,'$.finished_at') LIMIT 1000)",(now-86400,))
            if conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'provider_attempt:%'").fetchone()[0]>=MAX_RECORDS:
                raise UsageError('attempt_ledger_full')
        reservation=conn.execute('INSERT INTO provider_calls(provider,at) VALUES(?,?)',(provider,now)).lastrowid
        value={'id':str(uuid.uuid4()),'provider':provider,'operation':operation,'input_sha256':revision.lower() if revision else None,
               'input_kind':kind if kind in {'file_sha256','sample_sha256'} else 'unknown','state':'reserved',
               'reserved_at':now,'sent_at':None,'finished_at':None,'http_status':None,'code':None,
               'budget_reservation':reservation,'executor':PROCESS_ID,'budget_spent':True}
        conn.execute('INSERT INTO settings VALUES(?,?)',(PREFIX+value['id'],db.encode(value)))
        return value


def mark_sent(settings,identity):
    with db.transaction(settings.db_path) as conn:
        row=conn.execute('SELECT value FROM settings WHERE key=?',(PREFIX+identity,)).fetchone()
        if not row:raise UsageError('attempt_missing')
        value=json.loads(row[0])
        if value['state']!='reserved':raise UsageError('attempt_already_sent')
        value.update(state='sent',sent_at=time.time())
        conn.execute('UPDATE settings SET value=? WHERE key=?',(db.encode(value),PREFIX+identity))


def finish(settings,identity,state,code=None,status=None):
    if state not in STATES-{'reserved','sent'}:raise ValueError('Invalid attempt outcome')
    codes={'response_received','not_found','provider_rejected','rate_limited','provider_error','invalid_response','response_too_large','network_outcome_unknown','deadline_outcome_unknown','cancelled_outcome_unknown','not_sent','executor_lost'}
    with db.transaction(settings.db_path) as conn:
        row=conn.execute('SELECT value FROM settings WHERE key=?',(PREFIX+identity,)).fetchone()
        if not row:return
        value=json.loads(row[0])
        if value['state'] not in {'reserved','sent'}:return
        value.update(state=state,finished_at=time.time(),code=code if code in codes else 'provider_error',
                     http_status=status if type(status) is int and 100<=status<=599 else None)
        conn.execute('UPDATE settings SET value=? WHERE key=?',(db.encode(value),PREFIX+identity))


def report(settings,provider=None,state=None,page=1,page_size=50):
    filters=["key LIKE 'provider_attempt:%'"];params=[]
    if provider:filters.append("json_extract(value,'$.provider')=?");params.append(provider)
    if state:filters.append("json_extract(value,'$.state')=?");params.append(state)
    where=' AND '.join(filters)
    with db.connect(settings.db_path) as conn:
        total=conn.execute('SELECT COUNT(*) FROM settings WHERE '+where,params).fetchone()[0]
        rows=conn.execute('SELECT value FROM settings WHERE '+where+" ORDER BY json_extract(value,'$.reserved_at') DESC,key LIMIT ? OFFSET ?",(*params,page_size,(page-1)*page_size)).fetchall()
    fields=('id','provider','operation','input_sha256','input_kind','state','reserved_at','sent_at','finished_at','http_status','code','budget_spent')
    return {'items':[{key:value[key] for key in fields} for value in (json.loads(row[0]) for row in rows)],'total':total,'page':page,'page_size':page_size,
            'unknown_is_spent':True,'automatic_unknown_retry':False,'retention_days':30,'capacity':MAX_RECORDS}


def install(app,settings):
    @app.get('/api/reports/provider-attempts')
    def attempts(request:Request,provider:str|None=None,state:str|None=None,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=100)):
        security.require_session(request,settings)
        if provider is not None and provider not in (*PROVIDERS,'other') or state is not None and state not in STATES:
            raise security.error(422,'invalid_attempt_filter','Неизвестный источник или состояние попытки')
        return report(settings,provider,state,page,page_size)
