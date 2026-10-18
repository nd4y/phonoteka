"""Separate CPU/GPU inference process with durable idempotent jobs and one model."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from . import cue, media

ROOT=Path(os.environ.get('WORK_ROOT','/work'))
STATE=Path(os.environ.get('INFERENCE_STATE','/inference'))
MODEL=os.environ.get('ASR_MODEL','small')
MODELS={'small'}
MODEL_REVISION='536b0662742c02347bc0e980a01041f333bce120'
if MODEL not in MODELS: raise ValueError('Model must be selected by deployment from the registry')
SECRET=Path(os.environ.get('INFERENCE_SECRET_FILE','/run/secrets/inference_key'))
MODEL_INSTANCE=None


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


def identity(body): return hashlib.sha256(json.dumps([body,MODEL,'transcribe-v1'],sort_keys=True).encode()).hexdigest()


def execute(body):
    global MODEL_INSTANCE
    source=cue.safe_path(ROOT,body['relative_path'])
    media._expect(source,body['sha256'])
    info=media.probe_audio(source)
    if not 0<info['duration']<=1800: raise ValueError('ASR profile supports audio up to 30 minutes')
    if MODEL_INSTANCE is None:
        from faster_whisper import WhisperModel
        MODEL_INSTANCE=WhisperModel('/models/faster-whisper-small',device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,local_files_only=True)
    # Decode in a bounded, network-disabled ffmpeg process. Input timestamps remain unchanged.
    with tempfile.TemporaryDirectory(dir=STATE) as directory:
        pcm=Path(directory)/'analysis.wav'
        media._process(source,'ffmpeg',['-nostdin','-map','0:a:0','-t','1800','-ac','1','-ar','16000','-c:a','pcm_s16le','-fs',str(60*1024**2),str(pcm)],timeout=600)
        segments,info_asr=MODEL_INSTANCE.transcribe(str(pcm),language=body.get('language'),beam_size=5,word_timestamps=True,vad_filter=False,condition_on_previous_text=False)
        words=[];lines=[]
        for segment in segments:
            if segment.no_speech_prob>.6 or segment.avg_logprob<-1: continue
            lines.append({'start':segment.start,'end':segment.end,'text':segment.text,'avg_logprob':segment.avg_logprob,'no_speech_prob':segment.no_speech_prob})
            words.extend({'start':w.start,'end':w.end,'word':w.word,'probability':w.probability} for w in segment.words or [])
            if len(words)>20000: raise ValueError('ASR output exceeded word limit')
    media._expect(source,body['sha256'])
    return {'state':'draft','engine':'faster-whisper','engine_version':'1.2.1','model':MODEL,'model_revision':MODEL_REVISION,'language':info_asr.language,'duration':info['duration'],'input_sha256':body['sha256'],'words':words,'segments':lines,'text':'\n'.join(x['text'].strip() for x in lines),'automatic_acceptance':False,'timeline':'original_audio_seconds'}


async def worker():
    while True:
        with connect() as conn:
            row=conn.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row: conn.execute("UPDATE jobs SET state='running',updated=? WHERE id=?",(time.time(),row['id']))
        if not row:
            await asyncio.sleep(.5);continue
        try:
            result=await asyncio.to_thread(execute,json.loads(row['input']))
            state='succeeded'
        except Exception as exc:
            # Never persist credentials, request headers, or arbitrary model traceback.
            result={'code':getattr(exc,'code','inference_failed'),'message':str(exc)[:300] if isinstance(exc,(media.MediaError,ValueError)) else 'Inference failed; see private service diagnostics'};state='failed'
        with connect() as conn:
            conn.execute('UPDATE jobs SET state=?,result=?,updated=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False),time.time(),row['id']))


@asynccontextmanager
async def lifespan(app):
    STATE.mkdir(parents=True,exist_ok=True)
    with connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,input TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL,updated REAL NOT NULL)')
        conn.execute("UPDATE jobs SET state='interrupted' WHERE state='running'")
    task=asyncio.create_task(worker())
    yield
    task.cancel()


app=FastAPI(lifespan=lifespan)

@app.get('/health')
def health(): return {'status':'ok','model':MODEL,'model_revision':MODEL_REVISION,'capabilities':['transcribe'],'parallelism':1}

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
