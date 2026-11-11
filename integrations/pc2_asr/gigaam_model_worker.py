"""Generic disposable Russian recognizer. No client-application imports or reference text."""
import hashlib,importlib.metadata,json,os,pathlib,socket,sys,wave

MODEL='gigaam-v3-e2e-rnnt'
REVISION='gigaam-0.2.0-v3_e2e_rnnt'
WEIGHT_SHA256='f60c62fe45902d967000770a12f260de7c5ac4d4b8e5e852f1df8c548e3958b5'
VOCABULARY_SHA256='828c12c991019eef952a960661f25a92d6ad279591e2ea466b4aeddf1d20a18a'
RATE=16000

def chunk_ranges(samples):
    """Keep short-form inference bounded; cut at real quiet audio, never invent times."""
    import numpy as np
    pos=0;size=len(samples);maximum=round(23.5*RATE)
    while size-pos>maximum:
        first=pos+round(18*RATE);last=min(size,pos+maximum)
        width=round(.1*RATE)
        starts=range(first,max(first+1,last-width+1),width)
        cut=min(starts,key=lambda i:float(np.mean(np.square(samples[i:i+width]))))
        yield pos,cut
        pos=cut
    if pos<size:yield pos,size

def main():
    root=pathlib.Path(sys.argv[1]);request=json.loads((root/'request.json').read_text(encoding='utf-8'))
    if request.get('operation')!='transcribe' or request.get('language')!='ru' or request.get('text'):
        raise ValueError('Russian independent recognition profile')
    cache=pathlib.Path(request['model_directory'])
    for name,sha in [('v3_e2e_rnnt.ckpt',WEIGHT_SHA256),('v3_e2e_rnnt_tokenizer.model',VOCABULARY_SHA256)]:
        p=cache/name
        with p.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
        if actual!=sha:raise ValueError('Pinned model asset mismatch')
    if importlib.metadata.version('gigaam')!='0.2.0':raise ValueError('Pinned recognizer version')
    # Inference uses already present assets only. No request can download a model.
    def offline(*args,**kwargs):raise OSError('Model worker network disabled')
    socket.socket.connect=offline
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    if os.name=='nt':
        handle=os.add_dll_directory(request['cuda_dll_directory'])
        os.environ['PATH']=request['cuda_dll_directory']+os.pathsep+os.environ.get('PATH','')
    else:handle=None
    import numpy as np
    import gigaam
    with wave.open(str(root/'audio.wav'),'rb') as audio:
        if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate(),audio.getcomptype())!=(1,2,RATE,'NONE'):
            raise ValueError('PCM profile')
        duration=audio.getnframes()/RATE
        if not 0<duration<=1800:raise ValueError('Duration profile')
        samples=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2').copy()
    model=gigaam.load_model('v3_e2e_rnnt',download_root=str(cache),device='cuda')
    segments=[];words=[]
    for start,end in chunk_ranges(samples.astype(np.float32)/32768):
        clip=root/'chunk.wav'
        try:
            with wave.open(str(clip),'wb') as audio:
                audio.setparams((1,2,RATE,0,'NONE','NONE'));audio.writeframes(samples[start:end].tobytes())
            result=model.transcribe(str(clip),word_timestamps=True)
        finally:
            clip.unlink(missing_ok=True)
        current=[{'word':w.text,'start':float(w.start)+start/RATE,'end':float(w.end)+start/RATE}
                 for w in (result.words or []) if w.text.strip()]
        words.extend(current)
        if len(words)>20000:raise ValueError('Result word budget')
        if result.text.strip():
            segments.append({'start':start/RATE,'end':end/RATE,'text':result.text.strip(),'words':current})
        elif current:raise ValueError('Inconsistent recognizer result')
    result={'model':MODEL,'model_revision':REVISION,'model_sha256':WEIGHT_SHA256,
            'tokenizer_sha256':VOCABULARY_SHA256,'engine':'gigaam','engine_version':'0.2.0','language':'ru',
            'duration':duration,'text':'\n'.join(s['text'] for s in segments),'segments':segments,'words':words,
            'word_probabilities':False,'confidence_available':False,'device':'cuda','automatic_acceptance':False,
            'timeline':'original_audio_seconds'}
    raw=json.dumps(result,ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(raw)>8*1024**2:raise ValueError('Result byte budget')
    (root/'result.json').write_bytes(raw)
    if handle is not None:handle.close()

if __name__=='__main__':
    try:main()
    except ValueError:raise SystemExit(2)
