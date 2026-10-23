"""Pinned generic ASR engine over HTTPS; all music/line decisions remain local."""
import hashlib,io,json,math,os,re,wave
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from . import media

MODEL='faster-whisper-large-v3'
REVISION='edaa852ec7e145841d8ffdb056a99866b5f0a478'
MODEL_SHA256='69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1'
MAX_RESULT=8*1024**2

def _error(code):return media.MediaError('inference_pc2_'+code,'PC2 ASR '+code.replace('_',' '))

def validate_words(words,duration):
    if not isinstance(words,list) or len(words)>20000:raise _error('word_budget')
    previous=-1
    for word in words:
        if not isinstance(word,dict) or not isinstance(word.get('word'),str) or len(word['word'])>4096:raise _error('word_contract')
        values=[word.get(k) for k in ('start','end','probability')]
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):raise _error('word_contract')
        start,end,probability=values
        if not 0<=start<=end<=duration+.1 or start<previous or not 0<=probability<=1:raise _error('word_timeline')
        previous=start
    return words

def pcm_bytes(samples):
    import numpy as np
    out=io.BytesIO()
    with wave.open(out,'wb') as target:
        target.setparams((1,2,16000,0,'NONE','NONE'))
        target.writeframes(np.clip(np.rint(samples*32768),-32768,32767).astype('<i2').tobytes())
    return out.getvalue()

class Engine:
    def __init__(self,*,url=None,keyfile=None,transport=None):
        url=(url if url is not None else os.environ.get('PC2_ASR_URL','')).rstrip('/')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/')!='/v1/whisper':
            raise _error('configuration')
        key=Path(keyfile or os.environ.get('PC2_ASR_SECRET_FILE','/run/secrets/pc2_asr_key')).read_text(encoding='ascii').strip()
        if not 32<=len(key)<=256 or not re.fullmatch('[A-Za-z0-9_-]+',key):raise _error('configuration')
        self.client=httpx.Client(base_url=url+'/',headers={'Authorization':'Bearer '+key},timeout=httpx.Timeout(900,connect=10,pool=10),follow_redirects=False,trust_env=False,transport=transport)
    def __enter__(self):
        try:
            health=self.request('GET','health',timeout=15)
            self.identity(health)
            if health.get('word_probabilities') is not True or not {'transcribe','align_window'}<=set(health.get('capabilities',[])) or health.get('parallelism')!=1:
                raise _error('capabilities')
        except BaseException:
            self.client.close();raise
        return self
    def __exit__(self,*args):self.client.close()
    @staticmethod
    def identity(result):
        if not isinstance(result,dict) or result.get('model')!=MODEL or result.get('model_revision')!=REVISION or result.get('model_sha256')!=MODEL_SHA256:
            raise _error('model_identity')
    def request(self,method,path,**kwargs):
        try:
            with self.client.stream(method,path,**kwargs) as response:
                if response.status_code==422:raise _error('input_profile')
                if response.status_code in (429,503):raise _error('unavailable')
                if response.status_code!=200:raise _error('http_failure')
                raw=bytearray()
                for chunk in response.iter_bytes(64*1024):
                    if len(raw)+len(chunk)>MAX_RESULT:raise _error('result_budget')
                    raw.extend(chunk)
            return json.loads(raw)
        except (httpx.HTTPError,ValueError,UnicodeError) as exc:raise _error('transport') from exc
    def transcribe(self,path,language,duration):
        with Path(path).open('rb') as audio:
            digest=hashlib.file_digest(audio,'sha256').hexdigest();audio.seek(0)
            result=self.request('POST','audio/transcriptions',files={'file':('analysis.wav',audio,'audio/wav')},data={'language':language or ''})
        self.identity(result)
        if result.get('input_sha256')!=digest:raise _error('audio_identity')
        remote_duration=result.get('duration')
        if type(remote_duration) not in (int,float) or not math.isfinite(remote_duration) or abs(remote_duration-duration)>.1:raise _error('duration')
        detected=result.get('language')
        if not isinstance(detected,str) or not re.fullmatch('[a-z]{2,3}',detected) or (language and detected!=language):raise _error('language')
        validate_words(result.get('words'),duration)
        segments=result.get('segments')
        if not isinstance(segments,list) or len(segments)>20000:raise _error('segment_contract')
        words=[];lines=[]
        for segment in segments:
            if not isinstance(segment,dict) or not isinstance(segment.get('text'),str):raise _error('segment_contract')
            for field in ('start','end','no_speech_prob','avg_logprob'):
                value=segment.get(field)
                if type(value) not in (int,float) or not math.isfinite(value):raise _error('segment_contract')
            if not 0<=segment['start']<=segment['end']<=duration+.1 or not 0<=segment['no_speech_prob']<=1:raise _error('segment_contract')
            current=validate_words(segment.get('words'),duration)
            if segment['no_speech_prob']>.6 and segment['avg_logprob']<-1:continue
            words.extend(current)
            lines.append({k:segment[k] for k in ('start','end','text','avg_logprob','no_speech_prob')})
            lines[-1]['uncertain']=segment['no_speech_prob']>.6 or segment['avg_logprob']<-1
        validate_words(words,duration)
        return {'engine':'faster-whisper','engine_version':'1.2.1','model':MODEL,'model_revision':REVISION,'language':detected,'duration':duration,'words':words,'segments':lines,'text':'\n'.join(x['text'].strip() for x in lines),'device':'pc2_cuda','automatic_acceptance':False,'timeline':'original_audio_seconds'}
    def align_window(self,clip,text,language):
        audio=pcm_bytes(clip);digest=hashlib.sha256(audio).hexdigest();duration=len(clip)/16000
        try:result=self.request('POST','audio/alignments',files={'file':('window.wav',audio,'audio/wav')},data={'language':language,'text':text})
        except media.MediaError as exc:
            if exc.code=='inference_pc2_input_profile':return None
            raise
        self.identity(result)
        if result.get('input_sha256')!=digest or result.get('language')!=language:raise _error('audio_identity')
        actual=result.get('duration')
        if type(actual) not in (int,float) or not math.isfinite(actual) or abs(actual-duration)>.001:raise _error('duration')
        return validate_words(result.get('words'),duration)
