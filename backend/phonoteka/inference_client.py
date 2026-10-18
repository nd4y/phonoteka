import asyncio
import hashlib
import json
import time
import httpx
from . import db, media
from .alignment import align_lines


async def process(worker, job, item):
    settings=worker.settings
    if not settings.inference_url or not settings.inference_secret_file:
        raise media.MediaError('inference_disabled','ASR не настроен')
    params=json.loads(job['payload']).get('params',{})
    row=await worker.io(db.fetchone,settings.db_path,'SELECT * FROM tracks WHERE id=? AND is_current=1',(item['track_id'],))
    if not row or row['sha256']!=item['sha256']: raise media.MediaError('conflict','File revision changed')
    meta=json.loads(row['metadata'])
    if meta.get('input_error'): raise media.MediaError('invalid_audio','Cannot transcribe invalid input')
    token=(await worker.io(settings.inference_secret_file.read_text)).strip()
    body={'relative_path':item['relative_path'],'sha256':item['sha256'],'language':params.get('language') or None,'generation':params.get('generation',0)}
    async with httpx.AsyncClient(base_url=settings.inference_url,timeout=15,headers={'Authorization':'Bearer '+token}) as client:
        health=await client.get('/health');health.raise_for_status();model=health.json()['model'];revision=health.json()['model_revision']
        remote_id=hashlib.sha256(json.dumps([body,model,revision,'transcribe-v1'],sort_keys=True).encode()).hexdigest()
        worker.item_result(job['id'],item['track_id'],'running',{'inference_job_id':remote_id,'model':model})
        # The remote API deduplicates by the complete input contract, including model and generation.
        for attempt in range(2):
            try:
                submitted=await client.post('/jobs',json=body);submitted.raise_for_status()
                if submitted.json()['id']!=remote_id: raise media.MediaError('inference_contract','Remote job identity mismatch')
                break
            except httpx.HTTPError:
                status=await client.get('/jobs/'+remote_id)
                if status.status_code==200: break
                if attempt: raise media.MediaError('inference_unknown_outcome','Не удалось подтвердить состояние ASR')
                await asyncio.sleep(2)
        deadline=time.monotonic()+7200
        while time.monotonic()<deadline:
            if worker.cancelled(job['id']):
                await client.delete('/jobs/'+remote_id)
                raise media.MediaError('cancelled','ASR result was not accepted')
            try:
                response=await client.get('/jobs/'+remote_id);response.raise_for_status();state=response.json()
            except httpx.HTTPError:
                await asyncio.sleep(5);continue
            if state['state']=='succeeded': result=state['result'];break
            if state['state'] in {'failed','interrupted','cancelled'}:
                raise media.MediaError('inference_'+state['state'],(state.get('result') or {}).get('message','ASR did not complete'))
            await asyncio.sleep(2)
        else: raise media.MediaError('inference_timeout','ASR deadline reached; remote outcome must be reconciled')
    await worker.io(media._expect,settings.work_root/item['relative_path'],item['sha256'])
    if result.get('input_sha256')!=item['sha256']: raise media.MediaError('inference_contract','Wrong audio revision')
    aligned=None
    if job['kind']=='align':
        proposal=await worker.io(db.fetchone,settings.db_path,'SELECT payload FROM proposals WHERE track_id=? AND input_sha256=? ORDER BY created_at DESC LIMIT 1',(item['track_id'],item['sha256']))
        text=json.loads(proposal['payload']).get('lrc','') if proposal else ''
        text=text or meta.get('lrc') or meta.get('lyrics')
        if not text: raise media.MediaError('lyrics_required','Сначала выберите или сохраните текст')
        aligned=align_lines(text,result.get('words',[]),meta['duration'])
    if worker.cancelled(job['id']) or int(db.setting(settings.db_path,'auth_epoch','0'))!=job['auth_epoch']:
        raise media.MediaError('authorization_revoked','Analysis authorization revoked')
    with db.transaction(settings.db_path) as conn:
        current=conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1',(item['track_id'],)).fetchone()
        if not current or current['sha256']!=item['sha256']: raise media.MediaError('conflict','File revision changed')
        analysis=conn.execute('SELECT payload,input_sha256 FROM analyses WHERE track_id=?',(item['track_id'],)).fetchone()
        data=json.loads(analysis['payload']) if analysis and analysis['input_sha256']==item['sha256'] else {'candidates':[],'sources':[],'query_source':'audio','state':'needs_review','input_sha256':item['sha256']}
        data['asr']={**result,'remote_job_id':remote_id}
        if aligned:
            data['alignment']=aligned
            if aligned['lrc']:
                data['candidates']=[x for x in data['candidates'] if x['id']!='alignment:'+remote_id]
                data['candidates'].append({'id':'alignment:'+remote_id,'provider':'independent_asr','kind':'lyrics','title':'Проверить времена ASR','lrc':aligned['lrc'],'match':{'state':'needs_review','reasons':['human_timing_review_required']},'timing_verified':False})
        conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',(item['track_id'],item['sha256'],db.encode(data),db.utcnow()))
        db.event(conn,'analysis_completed',{'track_id':item['track_id'],'kind':job['kind']})
    return {'state':'needs_review','inference_job_id':remote_id,'words':len(result.get('words',[])),'alignment_coverage':aligned['coverage'] if aligned else None}
