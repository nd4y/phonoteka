"""Separate CPU/GPU inference process with durable idempotent jobs and one model."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import wave
from fastapi import FastAPI, Request, HTTPException, Response
from pydantic import BaseModel, Field
from . import cue, media
from .telemetry import Metrics, ObserveHTTP, metrics_authorized, CONTENT_TYPE_LATEST, log
from .metrics import DatabaseCollector
from .inference_budget import ModelProcess, BudgetError, PROFILE, MAX_DURATION, MAX_PCM_BYTES, MAX_SAMPLES, MAX_INPUT_CHANNELS, MAX_INPUT_RATE, profile

ROOT=Path(os.environ.get('WORK_ROOT','/work'))
STATE=Path(os.environ.get('INFERENCE_STATE','/inference'))
MODEL=os.environ.get('ASR_MODEL','small')
MODELS={'small'}
MODEL_REVISION='536b0662742c02347bc0e980a01041f333bce120'
if MODEL not in MODELS: raise ValueError('Model must be selected by deployment from the registry')
SECRET=Path(os.environ.get('INFERENCE_SECRET_FILE','/run/secrets/inference_key'))
MODEL_INSTANCE=None
LOG=logging.getLogger('phonoteka.inference')
METRICS=Metrics('inference')
METRICS.registry.register(DatabaseCollector(inference_path=STATE/'jobs.sqlite3'))
METRICS_SECRET=Path(os.environ['METRICS_TOKEN_FILE']) if os.environ.get('METRICS_TOKEN_FILE') else None


def connect():
    conn=sqlite3.connect(STATE/'jobs.sqlite3',timeout=10);conn.row_factory=sqlite3.Row;return conn


def auth(request):
    expected=SECRET.read_text().strip()
    if not expected or not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+expected):
        raise HTTPException(401,'Unauthorized inference request')


class Input(BaseModel):
    relative_path:str=Field(min_length=1,max_length=4096)
    sha256:str=Field(pattern='^[0-9a-f]{64}$')
    language:str|None=Field(default=None,pattern='^[a-z]{2,3}$')
    generation:int=Field(default=0,ge=0,le=1000000)
    reference_text:str|None=Field(default=None,max_length=100000)


def identity(body): return hashlib.sha256(json.dumps([body,MODEL,MODEL_REVISION,PROFILE,'transcribe-v3'],sort_keys=True).encode()).hexdigest()


def execute(body, on_model_loaded=None):
    global MODEL_INSTANCE
    source=cue.safe_path(ROOT,body['relative_path'])
    media._expect(source,body['sha256'])
    info=media.probe_audio(source)
    if not 0<info['duration']<=MAX_DURATION: raise BudgetError('inference_duration_budget','ASR profile supports audio up to 30 minutes')
    if not 0 < info.get('channels', 0) <= MAX_INPUT_CHANNELS or not 0 < info.get('sample_rate', 0) <= MAX_INPUT_RATE:
        raise BudgetError('inference_input_audio_budget','Source channel count or sample rate exceeds the CPU profile')
    if MODEL_INSTANCE is None:
        from faster_whisper import WhisperModel
        MODEL_INSTANCE=WhisperModel('/models/faster-whisper-small',device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,local_files_only=True)
        METRICS.model_loaded.set(1)
        log('inference_model_loaded')
    if on_model_loaded:
        on_model_loaded()
    # Decode in a bounded, network-disabled ffmpeg process. Input timestamps remain unchanged.
    with tempfile.TemporaryDirectory(dir=body.get('_scratch_root', STATE)) as directory:
        pcm=Path(directory)/'analysis.wav'
        media._process(source,'ffmpeg',['-nostdin','-map','0:a:0','-t',str(MAX_DURATION),'-ac','1','-ar','16000','-c:a','pcm_s16le','-fs',str(MAX_PCM_BYTES),str(pcm)],timeout=600)
        # Consume our validated PCM directly; do not decode twice through PyAV.
        import numpy as np
        with wave.open(str(pcm), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError('Unexpected analysis PCM profile')
            if not 0 < audio.getnframes() <= MAX_SAMPLES or pcm.stat().st_size > MAX_PCM_BYTES:
                raise BudgetError('inference_pcm_budget','Analysis PCM exceeds the selected resource profile')
            if abs(audio.getnframes()/16000 - info['duration']) > max(.5,info['duration']*.001):
                raise BudgetError('inference_duration_mismatch','Decoded timeline disagrees with the input duration')
            samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2').astype(np.float32)/32768.0
        segments,info_asr=MODEL_INSTANCE.transcribe(samples,language=body.get('language'),beam_size=5,word_timestamps=True,vad_filter=False,condition_on_previous_text=False)
        words=[];lines=[]
        for segment in segments:
            if segment.no_speech_prob>.6 and segment.avg_logprob<-1: continue
            lines.append({'start':segment.start,'end':segment.end,'text':segment.text,'avg_logprob':segment.avg_logprob,'no_speech_prob':segment.no_speech_prob,'uncertain':segment.no_speech_prob>.6 or segment.avg_logprob<-1})
            words.extend({'start':w.start,'end':w.end,'word':w.word,'probability':w.probability} for w in segment.words or [])
            if len(words)>20000: raise ValueError('ASR output exceeded word limit')
    alignment=None
    if body.get('reference_text'):
        from .forced_alignment import align_reference
        alignment=align_reference(MODEL_INSTANCE,samples,body['reference_text'],info_asr.language,words,info['duration'])
    media._expect(source,body['sha256'])
    return {'alignment':alignment,'state':'draft','engine':'faster-whisper','engine_version':'1.2.1','model':MODEL,'model_revision':MODEL_REVISION,'language':info_asr.language,'duration':info['duration'],'input_sha256':body['sha256'],'words':words,'segments':lines,'text':'\n'.join(x['text'].strip() for x in lines),'automatic_acceptance':False,'timeline':'original_audio_seconds'}


def model_rss(current, peak):
    METRICS.model_rss.set(current)
    METRICS.model_peak.set(peak)


MODEL_WORKER = ModelProcess(on_residency=lambda loaded: METRICS.model_loaded.set(int(loaded)),
                           on_rss=model_rss, on_start=METRICS.model_starts.inc, scratch_root=STATE)


async def worker():
    while True:
        with connect() as conn:
            row=conn.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row: conn.execute("UPDATE jobs SET state='running',updated=? WHERE id=?",(time.time(),row['id']))
        if not row:
            await asyncio.sleep(.5);continue
        started=time.perf_counter()
        log('inference_started',job_id=row['id'])
        try:
            result=await asyncio.to_thread(MODEL_WORKER.run,json.loads(row['input']))
            state='succeeded'
        except Exception as exc:
            LOG.exception('Inference job failed',extra={'event':'inference_failed','fields':{'job_id':row['id']}})
            # Never persist credentials, request headers, or arbitrary model traceback.
            result={'code':getattr(exc,'code','inference_failed'),'message':str(exc)[:300] if isinstance(exc,(media.MediaError,ValueError)) else 'Inference failed; see private service diagnostics'};state='failed'
        with connect() as conn:
            conn.execute('UPDATE jobs SET state=?,result=?,updated=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False),time.time(),row['id']))
        duration=time.perf_counter()-started
        METRICS.inference_jobs.labels(state).inc();METRICS.inference_duration.observe(duration)
        log('inference_finished',job_id=row['id'],state=state,duration_seconds=round(duration,6))


@asynccontextmanager
async def lifespan(app):
    STATE.mkdir(parents=True,exist_ok=True)
    with connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,input TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL,updated REAL NOT NULL)')
        conn.execute("UPDATE jobs SET state='interrupted' WHERE state='running'")
    task=asyncio.create_task(worker())
    log('service_ready')
    try:
        yield
    finally:
        log('service_stopping')
        MODEL_WORKER.request_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            # Cancelling to_thread does not stop its OS thread. The lifecycle
            # lock drains that owner before handles and scratch are released.
            await asyncio.to_thread(MODEL_WORKER.close)


app=FastAPI(lifespan=lifespan)
app.add_middleware(ObserveHTTP,metrics=METRICS)

@app.get('/metrics',include_in_schema=False)
def metrics(request:Request):
    if not metrics_authorized(request.headers,METRICS_SECRET): raise HTTPException(401,'Metrics token required')
    return Response(METRICS.render(),media_type=CONTENT_TYPE_LATEST,headers={'Cache-Control':'no-store'})

@app.get('/health')
def health(): return {'status':'ok','model':MODEL,'model_revision':MODEL_REVISION,'profile_revision':PROFILE,'resource_profile':profile(),'capabilities':['transcribe','align'],'contract':'transcribe-v3','parallelism':1}

@app.post('/jobs',status_code=202)
def submit(body:Input,request:Request):
    auth(request);data=body.model_dump();key=identity(data)
    cue.safe_path(ROOT,body.relative_path)
    with connect() as conn:
        count=conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
        if count>=100 and not conn.execute('SELECT id FROM jobs WHERE id=?',(key,)).fetchone(): raise HTTPException(429,'Inference queue full')
        conn.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,'queued',NULL,?,?)",(key,json.dumps(data),time.time(),time.time()))
    return {'id':key}

@app.get('/jobs/{key}')
def status(key:str,request:Request):
    auth(request)
    with connect() as conn: row=conn.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
    if not row: raise HTTPException(404,'Unknown inference job')
    return {'id':key,'state':row['state'],'result':json.loads(row['result']) if row['result'] else None}

@app.delete('/jobs/{key}')
def cancel(key:str,request:Request):
    auth(request)
    with connect() as conn:
        row=conn.execute('SELECT state FROM jobs WHERE id=?',(key,)).fetchone()
        if not row: raise HTTPException(404,'Unknown inference job')
        if row['state']=='running': raise HTTPException(409,'Running inference finishes safely; its result need not be accepted')
        conn.execute("UPDATE jobs SET state='cancelled' WHERE id=? AND state='queued'",(key,))
    return {'id':key,'state':'cancelled'}
