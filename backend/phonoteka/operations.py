"""Authenticated diagnostics without track names, paths, lyrics or secrets."""
import json
import shutil
import time
from fastapi import Request
from fastapi.responses import JSONResponse
from . import __version__,db,security


def snapshot(settings):
    heartbeat=json.loads(db.setting(settings.db_path,'worker_heartbeat','{}'))
    storage={}
    for name,path in {'work':settings.work_root,'publication':settings.publication_root,'state':settings.state_dir}.items():
        try:
            usage=shutil.disk_usage(path);storage[name]={'total':usage.total,'free':usage.free}
        except OSError:storage[name]={'unavailable':True}
    return {'version':__version__,'schema':db.SCHEMA_VERSION,'created_at':db.utcnow(),
            'recovery_required':db.setting(settings.db_path,'recovery_required','false')=='true',
            'worker':{key:heartbeat.get(key) for key in ('at','state','file_workers','io_threads')},
            'worker_age_seconds':round(time.time()-heartbeat['at'],1) if heartbeat.get('at') else None,
            'jobs':db.fetchall(settings.db_path,'SELECT kind,state,stage,COUNT(*) AS count FROM jobs GROUP BY kind,state,stage'),
            'provider_calls_24h':db.fetchall(settings.db_path,'SELECT provider,COUNT(*) AS count FROM provider_calls WHERE at>? GROUP BY provider',(time.time()-86400,)),
            'provider_cache':db.fetchone(settings.db_path,'SELECT COUNT(*) AS entries,COALESCE(SUM(length(payload)),0) AS bytes FROM provider_cache'),
            'storage':storage,'rollback_retention':'keep_all','music_backup_included':False,
            'last_state_backup':json.loads(db.setting(settings.db_path,'last_state_backup','null'))}


def install(app,settings):
    @app.get('/api/reports/operations')
    def report(request:Request):
        security.require_session(request,settings)
        return JSONResponse(snapshot(settings),headers={'Content-Disposition':'attachment; filename="phonoteka-diagnostics.json"'})
