"""Authenticated optional model routes for the existing recap-asr service.

The shared engine's GPU lock also covers these disposable model workers.
Client policy, music metadata, database and reference-line decisions stay outside.
"""
import asyncio,contextlib,hashlib,hmac,json,os,pathlib,re,subprocess,tempfile,time,wave
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter,Gauge,Histogram,REGISTRY

MODEL='faster-whisper-large-v3'
REVISION='edaa852ec7e145841d8ffdb056a99866b5f0a478'
MODEL_SHA256='69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1'
PREFIX='/v1/whisper'
UPLOAD_LIMIT=60*1024**2
WIRE_LIMIT=UPLOAD_LIMIT+128*1024
RESULT_LIMIT=8*1024**2
RUSSIAN_MODEL='gigaam-v3-e2e-rnnt'
RUSSIAN_REVISION='gigaam-0.2.0-v3_e2e_rnnt'
RUSSIAN_SHA256='f60c62fe45902d967000770a12f260de7c5ac4d4b8e5e852f1df8c548e3958b5'
RUSSIAN_VOCABULARY_SHA256='828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a'

async def cleanup_temporary(temporary):
    """Windows may briefly retain inherited log handles after worker-tree reap."""
    delays=(0,.1,.3,1,2,4)
    for attempt,delay in enumerate(delays):
        if delay:await asyncio.sleep(delay)
        try:await asyncio.to_thread(temporary.cleanup)
        except PermissionError:
            if attempt==len(delays)-1:raise
        else:return

def install(app,engine,registry=REGISTRY):
    root=pathlib.Path(__file__).resolve().parent
    keyfile=root/'whisper-private/api_key'
    python=pathlib.Path(os.environ.get('RECAP_WHISPER_PYTHON','D:/Phonoteka-ASR/venv/Scripts/python.exe'))
    modeldir=pathlib.Path(os.environ.get('RECAP_WHISPER_MODEL_DIR','D:/Phonoteka-ASR/models/faster-whisper-large-v3'))
    dlls=root/'venv/Lib/site-packages/torch/lib'
    # This is checked before enabling the extra routes, not downloaded by requests.
    manifest=json.loads((modeldir.parents[1]/'model-manifest.json').read_text(encoding='utf-8'))
    if manifest['revision']!=REVISION or manifest['files']['model.bin']['sha256']!=MODEL_SHA256:
        raise RuntimeError('Pinned model manifest mismatch')
    if not python.is_file() or not (modeldir/'model.bin').is_file() or not keyfile.is_file():
        raise RuntimeError('Whisper engine configuration incomplete')
    key=keyfile.read_text(encoding='ascii').strip()
    if len(key)<32:raise RuntimeError('Whisper engine key is too short')
    scratch=root/'whisper-scratch';scratch.mkdir(mode=0o700,exist_ok=True)
    russian_python=root/'venv/Scripts/python.exe'
    russian_cache=pathlib.Path(os.environ.get('RECAP_GIGAAM_CACHE',str(root/'gigaam-cache')))
    russian_available=russian_python.is_file() and (root/'gigaam_model_worker.py').is_file()
    try:
        for name,sha in [('v3_e2e_rnnt.ckpt',RUSSIAN_SHA256),('v3_e2e_rnnt_tokenizer.model',RUSSIAN_VOCABULARY_SHA256)]:
            with (russian_cache/name).open('rb') as source:
                russian_available=russian_available and hashlib.file_digest(source,'sha256').hexdigest()==sha
    except OSError:russian_available=False
    pending=0
    inflight=Gauge('recap_whisper_inflight','Whisper running or waiting requests',registry=registry)
    active=Gauge('recap_whisper_active','Owned disposable GPU process',registry=registry)
    requests=Counter('recap_whisper_requests_total','Whisper terminal outcomes',['operation','result'],registry=registry)
    timings=Histogram('recap_whisper_seconds','Whisper request wall time',['operation'],registry=registry)

    @app.middleware('http')
    async def boundary(request,call_next):
        if not request.url.path.startswith(PREFIX+'/'):return await call_next(request)
        if not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+key):
            return JSONResponse({'error':'authentication_required'},status_code=401)
        length=request.headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length)>WIRE_LIMIT):
            return JSONResponse({'error':'upload_budget'},status_code=413)
        original=request._receive;received=0
        async def receive():
            nonlocal received
            message=await original();received+=len(message.get('body',b''))
            if received>WIRE_LIMIT:raise HTTPException(413,'upload_budget')
            return message
        request._receive=receive
        response=await call_next(request);response.headers['Cache-Control']='no-store'
        return response

    @app.get(PREFIX+'/health')
    def health():
        recognizers={RUSSIAN_MODEL:{'model':RUSSIAN_MODEL,'model_revision':RUSSIAN_REVISION,'model_sha256':RUSSIAN_SHA256,
            'tokenizer_sha256':RUSSIAN_VOCABULARY_SHA256,'language':'ru','word_timestamps':True,
            'word_probabilities':False,'capabilities':['transcribe']}} if russian_available else {}
        return {'status':'ok','model':MODEL,'model_revision':REVISION,'model_sha256':MODEL_SHA256,'engine':'faster-whisper','engine_version':'1.2.1','capabilities':['transcribe','align_window'],'device':'cuda','compute_type':'float16','parallelism':1,'max_duration':1800,'max_alignment_window':29.5,'max_alignment_tokens':400,'word_probabilities':True,'shared_engine':'recap-asr','pending':pending,'recognizers':recognizers}

    async def run(request,operation):
        nonlocal pending
        if pending>=4:raise HTTPException(429,'queue_budget')
        pending+=1;inflight.inc();start=time.monotonic();locked=False;process=None;temporary=None;outcome='error';disconnect_task=None
        disconnected=asyncio.Event()
        async def watch_disconnect():
            while True:
                message=await request.receive()
                if message['type']=='http.disconnect':disconnected.set();return
        try:
            async with request.form(max_files=1,max_fields=6,max_part_size=128*1024) as form:
                upload=form.get('file');language=form.get('language') or None;text=form.get('text','')
                requested_model=form.get('model') or MODEL
                if requested_model not in (MODEL,RUSSIAN_MODEL):raise HTTPException(422,'model_profile')
                russian=requested_model==RUSSIAN_MODEL
                if russian:
                    if not russian_available:raise HTTPException(503,'russian_model_unavailable')
                    if operation!='transcribe' or language!='ru' or text:raise HTTPException(422,'russian_transcription_profile')
                if upload is None or not hasattr(upload,'read'):raise HTTPException(422,'audio_required')
                if language is not None and (not isinstance(language,str) or not re.fullmatch('[a-z]{2,3}',language)):
                    raise HTTPException(422,'language_profile')
                if not isinstance(text,str) or len(text)>12000:raise HTTPException(422,'text_budget')
                if operation=='align' and (not text.strip() or not language):raise HTTPException(422,'alignment_reference_required')
                temporary=tempfile.TemporaryDirectory(prefix='recap-whisper-',dir=scratch)
                with contextlib.nullcontext(temporary.name) as directory:
                    job=pathlib.Path(directory);source=job/'audio.wav';size=0
                    with source.open('xb') as destination:
                        while chunk:=await upload.read(2*1024**2):
                            size+=len(chunk)
                            if size>UPLOAD_LIMIT:raise HTTPException(413,'upload_budget')
                            destination.write(chunk)
                    try:
                        with wave.open(str(source),'rb') as audio:
                            valid=(audio.getnchannels(),audio.getsampwidth(),audio.getframerate(),audio.getcomptype())==(1,2,16000,'NONE')
                            duration=audio.getnframes()/16000
                        if not valid or not 0<duration<=(29.5 if operation=='align' else 1800):raise ValueError()
                    except (wave.Error,EOFError,ValueError,OSError):raise HTTPException(422,'pcm_profile')
                    # Actively await the disconnect message after consuming the
                    # multipart body. Nonblocking is_disconnected polling loses
                    # notifications through middleware/proxy receive channels.
                    disconnect_task=asyncio.create_task(watch_disconnect())
                    while not engine._lock.acquire(blocking=False):
                        if time.monotonic()-start>90:raise HTTPException(503,'gpu_busy')
                        if disconnected.is_set():raise HTTPException(499,'client_disconnected')
                        await asyncio.sleep(.2)
                    locked=True
                    # Release only this engine's idle models while holding its lock.
                    engine._unload_models('before whisper request')
                    if hasattr(engine,'torch') and engine.torch.cuda.mem_get_info()[0]<7*1024**3:
                        raise HTTPException(503,'gpu_busy')
                    body={'operation':operation,'language':language,'text':text,'model_directory':str(russian_cache if russian else modeldir),'cuda_dll_directory':str(dlls)}
                    (job/'request.json').write_text(json.dumps(body,ensure_ascii=False),encoding='utf-8')
                    with (job/'worker.log').open('wb') as log:
                        allowed={'SYSTEMROOT','WINDIR','TEMP','TMP','PATH','PATHEXT','USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMDATA','CUDA_PATH','CUDA_VISIBLE_DEVICES'}
                        child_env={k:v for k,v in os.environ.items() if k.upper() in allowed}
                        child_env.update(OMP_NUM_THREADS='2',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
                        process=subprocess.Popen([str(russian_python if russian else python),str(root/('gigaam_model_worker.py' if russian else 'whisper_model_worker.py')),str(job)],stdout=log,stderr=subprocess.STDOUT,env=child_env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        active.set(1)
                        while process.poll() is None:
                            if time.monotonic()-start>900:raise HTTPException(504,'model_deadline')
                            if disconnected.is_set():raise HTTPException(499,'client_disconnected')
                            await asyncio.sleep(.2)
                    if process.returncode==2:raise HTTPException(422,'model_input_profile')
                    if process.returncode:raise HTTPException(503,'model_execution_failed')
                    output=job/'result.json'
                    if not output.is_file() or not 0<output.stat().st_size<=RESULT_LIMIT:raise HTTPException(502,'result_budget')
                    result=json.loads(output.read_text(encoding='utf-8'))
                    expected_model,expected_revision,expected_sha=(RUSSIAN_MODEL,RUSSIAN_REVISION,RUSSIAN_SHA256) if russian else (MODEL,REVISION,MODEL_SHA256)
                    if result.get('model_revision')!=expected_revision or result.get('model')!=expected_model:raise HTTPException(502,'model_identity')
                    if russian and (result.get('tokenizer_sha256')!=RUSSIAN_VOCABULARY_SHA256 or result.get('word_probabilities') is not False):raise HTTPException(502,'russian_model_identity')
                    result.update(input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),model_sha256=expected_sha,processing_seconds=round(time.monotonic()-start,4))
                    outcome='ok';return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except HTTPException as error:
            outcome='cancelled' if error.status_code==499 else 'rejected' if error.status_code in (413,422,429) else 'error'
            raise
        finally:
            async def cleanup():
                nonlocal pending
                try:
                    if process is not None and process.poll() is None:
                        if os.name=='nt':
                            # Windows venv python.exe is a launcher; terminating
                            # it alone leaves the actual model child alive.
                            killed=await asyncio.to_thread(subprocess.run,['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=5)
                            if killed.returncode and process.poll() is None:raise RuntimeError('owned_worker_tree_cleanup_failed')
                        else:process.terminate()
                        try:await asyncio.to_thread(process.wait,5)
                        except subprocess.TimeoutExpired:
                            process.kill();await asyncio.to_thread(process.wait,5)
                finally:
                    if disconnect_task is not None:
                        disconnect_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):await disconnect_task
                    try:
                        if temporary is not None:await cleanup_temporary(temporary)
                    finally:
                        active.set(0)
                        if locked:engine._lock.release()
                        pending-=1;inflight.dec();requests.labels(operation,outcome).inc();timings.labels(operation).observe(time.monotonic()-start)
            # Middleware can cancel repeatedly on disconnect. Reap the worker and
            # release the shared lock before propagating cancellation to callers.
            import anyio
            with anyio.CancelScope(shield=True):
                cleanup_task=asyncio.create_task(cleanup())
                cancelled=False
                while not cleanup_task.done():
                    try:await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:cancelled=True
                cleanup_task.result()
            if cancelled:raise asyncio.CancelledError

    @app.post(PREFIX+'/audio/transcriptions')
    async def transcriptions(request:Request):return await run(request,'transcribe')

    @app.post(PREFIX+'/audio/alignments')
    async def alignments(request:Request):return await run(request,'align')
