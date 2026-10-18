import asyncio
import hashlib
import json
import time
import httpx
from . import db, media, provider_policy, lyrics_document


async def process(worker, job, item):
    await worker.io(worker.item_action,job,item,'align' if job['kind']=='align' else 'asr')
    settings=worker.settings
    if not settings.inference_url or not settings.inference_secret_file:
        raise media.MediaError('inference_disabled','ASR не настроен')
    params=json.loads(job['payload']).get('params',{})
    row=await worker.io(db.fetchone,settings.db_path,'SELECT * FROM tracks WHERE id=? AND is_current=1',(item['track_id'],))
    if not row or row['sha256']!=item['sha256']: raise media.MediaError('conflict','File revision changed')
    meta=json.loads(row['metadata'])
    if meta.get('input_error'): raise media.MediaError('invalid_audio','Cannot transcribe invalid input')
    if job['kind']=='align':
        with db.connect(settings.db_path) as conn:
            reference=item.get('lyrics_reference')
            if reference and lyrics_document.effective(conn,row)['words_revision']!=reference['words_revision']:
                raise media.MediaError('lyrics_document_stale','Слова изменились после постановки alignment в очередь')
            provider_policy.require_payload(conn,row,{'lrc':item.get('reference_text',''),'field_sources':item.get('reference_sources',{})},'derived_analysis')
    token=(await worker.io(settings.inference_secret_file.read_text)).strip()
    body={'relative_path':item['relative_path'],'sha256':item['sha256'],'language':params.get('language') or None,'generation':item.get('generation',params.get('generation',0)),'reference_text':item.get('reference_text') if job['kind']=='align' else None}
    async with httpx.AsyncClient(base_url=settings.inference_url,timeout=15,headers={'Authorization':'Bearer '+token}) as client:
        health=await client.get('/health');health.raise_for_status();model=health.json()['model'];revision=health.json()['model_revision']
        if health.json().get('contract')!='transcribe-v3' or not health.json().get('profile_revision'): raise media.MediaError('inference_contract','Update app and inference to compatible versions')
        remote_id=hashlib.sha256(json.dumps([body,model,revision,health.json()['profile_revision'],'transcribe-v3'],sort_keys=True).encode()).hexdigest()
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
            if job['kind']=='align':
                try:
                    with db.connect(settings.db_path) as conn:
                        provider_policy.require_payload(conn,row,{'lrc':item.get('reference_text',''),'field_sources':item.get('reference_sources',{})},'derived_analysis')
                except media.MediaError:
                    await client.delete('/jobs/'+remote_id)
                    raise
            if worker.cancelled(job['id']):
                await client.delete('/jobs/'+remote_id)
                raise media.MediaError('cancelled','ASR result was not accepted')
            try:
                response=await client.get('/jobs/'+remote_id);response.raise_for_status();state=response.json()
            except httpx.HTTPError:
                await asyncio.sleep(5);continue
            if state['state']=='succeeded': result=state['result'];break
            if state['state'] in {'failed','interrupted','cancelled'}:
                result_error = state.get('result') or {}
                raise media.MediaError(result_error.get('code','inference_'+state['state']), result_error.get('message','ASR did not complete'))
            await asyncio.sleep(2)
        else: raise media.MediaError('inference_timeout','ASR deadline reached; remote outcome must be reconciled')
    await worker.io(media._expect,settings.work_root/item['relative_path'],item['sha256'])
    if result.get('input_sha256')!=item['sha256']: raise media.MediaError('inference_contract','Wrong audio revision')
    aligned=result.pop('alignment',None)
    if job['kind']=='align' and not aligned: raise media.MediaError('inference_contract','Alignment result missing')
    if worker.cancelled(job['id']) or int(db.setting(settings.db_path,'auth_epoch','0'))!=job['auth_epoch']:
        raise media.MediaError('authorization_revoked','Analysis authorization revoked')
    with db.transaction(settings.db_path) as conn:
        current=conn.execute('SELECT sha256 FROM tracks WHERE id=? AND is_current=1',(item['track_id'],)).fetchone()
        if not current or current['sha256']!=item['sha256']: raise media.MediaError('conflict','File revision changed')
        analysis=conn.execute('SELECT payload,input_sha256 FROM analyses WHERE track_id=?',(item['track_id'],)).fetchone()
        data=json.loads(analysis['payload']) if analysis and analysis['input_sha256']==item['sha256'] else {'candidates':[],'sources':[],'query_source':'audio','state':'needs_review','input_sha256':item['sha256']}
        data['asr']={**result,'remote_job_id':remote_id,'audio_identity':(meta.get('audit') or {}).get('audio_identity')}
        if aligned:
            reference=item.get('lyrics_reference')
            if reference:
                aligned.update(document_id=reference['document_id'],words_revision=reference['words_revision'])
                if len(aligned['lines'])==len(reference['lines']):
                    for line,source in zip(aligned['lines'],reference['lines']):
                        line.update(line_id=source['line_id'],original_ms=source['original_ms'])
                else:
                    aligned.update(stale=True,stale_reason='alignment_occurrence_mismatch')
                current_document=lyrics_document.effective(conn,row)
                if current_document['words_revision']!=reference['words_revision']:
                    aligned.update(stale=True,stale_reason='lyrics_words_changed')
            data['alignment']=aligned
            data['alignment_usage']=provider_policy.references({'field_sources':item.get('reference_sources',{})})
            if aligned['lrc'] and not aligned.get('stale'):
                data['candidates']=[x for x in data['candidates'] if x['id']!='alignment:'+remote_id]
                candidate={'id':'alignment:'+remote_id,'provider':'independent_asr','kind':'lyrics','title':'Проверить времена ASR','lrc':aligned['lrc'],'match':{'state':'needs_review','reasons':['human_timing_review_required']},'timing_verified':False}
                reference=item.get('reference_sources',{}).get('lrc',{})
                if reference.get('usage'):
                    usage=reference['usage'];candidate['usage']={**provider_policy.candidate_usage(conn,candidate),'origin':'provider_acquired','inherited_restrictions':usage if isinstance(usage,list) else [usage]}
                data['candidates'].append(candidate)
        data=provider_policy.prepare_analysis(conn,data)
        conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',(item['track_id'],item['sha256'],db.encode(data),db.utcnow()))
        db.event(conn,'analysis_completed',{'track_id':item['track_id'],'kind':job['kind']})
    return {'state':'needs_review','inference_job_id':remote_id,'words':len(result.get('words',[])),'alignment_coverage':aligned['coverage'] if aligned else None}
