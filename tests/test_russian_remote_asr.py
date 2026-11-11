"""Russian recognition is independent; only the existing aligner supplies final likelihood."""
import copy,hashlib,io,json,os,subprocess,sys,wave
import httpx
import pytest
from phonoteka import media,remote_asr
from phonoteka.forced_alignment import align_with_provider

@pytest.fixture
def russian(tmp_path):
    key=tmp_path/'key';key.write_text('k'*40)
    raw=io.BytesIO()
    with wave.open(raw,'wb') as w:
        w.setparams((1,2,16000,0,'NONE','NONE'));w.writeframes(b'\0\0'*16000)
    path=tmp_path/'audio.wav';path.write_bytes(raw.getvalue())
    identity={'model':remote_asr.RUSSIAN_MODEL,'model_revision':remote_asr.RUSSIAN_REVISION,
              'model_sha256':remote_asr.RUSSIAN_SHA256,'tokenizer_sha256':remote_asr.RUSSIAN_VOCABULARY_SHA256}
    recognizer={**identity,'word_timestamps':True,'word_probabilities':False,'language':'ru','capabilities':['transcribe']}
    health={'model':remote_asr.MODEL,'model_revision':remote_asr.REVISION,'model_sha256':remote_asr.MODEL_SHA256,
            'parallelism':1,'word_probabilities':True,'capabilities':['transcribe','align_window'],
            'recognizers':{remote_asr.RUSSIAN_MODEL:recognizer}}
    words=[{'word':'добрый','start':.1,'end':.3},{'word':'день','start':.4,'end':.7}]
    body={**identity,'duration':1,'language':'ru','word_probabilities':False,
          'input_sha256':hashlib.sha256(raw.getvalue()).hexdigest(),'words':words,
          'segments':[{'start':0,'end':1,'text':'добрый день','words':words}]}
    return key,path,health,body

def provider(fixture,change=None,health_change=None):
    key,_,health,body=copy.deepcopy(fixture);seen=[]
    if health_change:health_change(health)
    def handler(request):
        if request.url.path.endswith('/health'):return httpx.Response(200,json=health)
        seen.append(request.read())
        assert b'name="model"\r\n\r\ngigaam-v3-e2e-rnnt' in seen[-1]
        value=copy.deepcopy(body)
        if change:change(value)
        return httpx.Response(200,json=value)
    return remote_asr.Engine(url='https://engine.test/v1/whisper',keyfile=key,
        russian_model=remote_asr.RUSSIAN_MODEL,transport=httpx.MockTransport(handler)),seen

def test_explicit_russian_uses_real_unscored_word_times(russian):
    engine,seen=provider(russian)
    with engine:result=engine.transcribe(russian[1],'ru',1)
    assert len(seen)==1 and result['engine']=='gigaam' and result['language']=='ru'
    assert result['words']==russian[3]['words']
    assert all('probability' not in word for word in result['words'])
    assert all(segment['uncertain'] and not segment['confidence_available'] for segment in result['segments'])
    assert result['word_probabilities'] is False and result['automatic_acceptance'] is False

@pytest.mark.parametrize('change,code',[
    (lambda v:v.update(input_sha256='0'*64),'audio_identity'),
    (lambda v:v.update(tokenizer_sha256='0'*64),'russian_model_identity'),
    (lambda v:v.update(duration=2),'duration'),
    (lambda v:v.update(language='en'),'language'),
    (lambda v:v.update(word_probabilities=True),'russian_capabilities'),
    (lambda v:v['words'][0].update(probability=.99),'word_contract'),
    (lambda v:v['words'][0].update(start=True),'word_contract'),
    (lambda v:v['words'][0].update(end=2),'word_timeline'),
    (lambda v:v['words'].reverse(),'word_timeline'),
    (lambda v:v.update(words=[]),'word_contract'),
])
def test_russian_result_cannot_bypass_contract(russian,change,code):
    engine,_=provider(russian,change)
    with engine,pytest.raises(media.MediaError) as error:engine.transcribe(russian[1],'ru',1)
    assert error.value.code=='inference_pc2_'+code

def test_unavailable_russian_model_is_rejected_before_audio_upload(russian):
    engine,seen=provider(russian,health_change=lambda h:h.update(recognizers={}))
    with engine,pytest.raises(media.MediaError) as error:engine.transcribe(russian[1],'ru',1)
    assert error.value.code=='inference_pc2_russian_model_identity' and not seen

def test_optional_russian_model_does_not_break_english(russian):
    key,path,health,body=copy.deepcopy(russian);health['recognizers']={};seen=[]
    words=[{'word':'good','start':.1,'end':.3,'probability':.9},{'word':'day','start':.4,'end':.7,'probability':.95}]
    english={**{k:health[k] for k in ('model','model_revision','model_sha256')},'language':'en','duration':1,
             'input_sha256':body['input_sha256'],'words':words,
             'segments':[{'text':'good day','start':.1,'end':.7,'words':words,'no_speech_prob':.01,'avg_logprob':-.1}]}
    def handler(request):
        if request.url.path.endswith('/health'):return httpx.Response(200,json=health)
        seen.append(request.read());return httpx.Response(200,json=english)
    with remote_asr.Engine(url='https://engine.test/v1/whisper',keyfile=key,russian_model=remote_asr.RUSSIAN_MODEL,
                          transport=httpx.MockTransport(handler)) as engine:
        result=engine.transcribe(path,'en',1)
    assert result['engine']=='faster-whisper' and len(seen)==1
    assert b'name="model"' not in seen[0]

def test_automatic_russian_detection_uses_a_distinct_recognition_pass(russian):
    key,path,health,body=copy.deepcopy(russian);seen=[]
    scored=[{**word,'probability':.9} for word in body['words']]
    detected={**{k:health[k] for k in ('model','model_revision','model_sha256')},'language':'ru','duration':1,
              'input_sha256':body['input_sha256'],'words':scored,
              'segments':[{'text':'добрый день','start':.1,'end':.7,'words':scored,'no_speech_prob':.01,'avg_logprob':-.1}]}
    def handler(request):
        if request.url.path.endswith('/health'):return httpx.Response(200,json=health)
        raw=request.read();seen.append(raw)
        return httpx.Response(200,json=body if b'name="model"' in raw else detected)
    with remote_asr.Engine(url='https://engine.test/v1/whisper',keyfile=key,russian_model=remote_asr.RUSSIAN_MODEL,
                          transport=httpx.MockTransport(handler)) as engine:
        result=engine.transcribe(path,None,1)
    assert len(seen)==2 and result['engine']=='gigaam'
    assert all('probability' not in word for word in result['words'])

def test_recognition_profile_changes_cached_job_identity_without_changing_budgets():
    values=[]
    for selection in ('',remote_asr.RUSSIAN_MODEL):
        env={**os.environ,'ASR_BACKEND':'pc2','ASR_MODEL':remote_asr.MODEL,'PC2_ASR_RUSSIAN_MODEL':selection}
        code="import json;from phonoteka.inference import health,identity; print(json.dumps({'health':health(),'id':identity({'relative_path':'example.wav','sha256':'0'*64,'language':'ru'})}))"
        r=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=20)
        assert r.returncode==0,r.stderr
        values.append(json.loads(r.stdout))
    old,new=values
    assert old['id']!=new['id'] and old['health']['profile_revision']!=new['health']['profile_revision']
    a,b=old['health']['resource_profile'],new['health']['resource_profile']
    assert a['resident_models']==b['resident_models']==0 and a['model_process_rss_bytes']==b['model_process_rss_bytes']
    assert any('gigaam' in name for name in b['models'])

def test_gigaam_anchors_still_require_real_alignment_confidence(russian):
    words=russian[3]['words'];scored=[{**w,'probability':.9} for w in words]
    def align(found):return align_with_provider([0]*16000,'добрый день',words,1,
        lambda clip,text:copy.deepcopy(found),unscored_words=True)
    good=align(scored)
    assert good['coverage']==1 and good['independent_word_confidence_available'] is False
    assert good['automatic_acceptance'] is False and good['music_quality_gate']=='not_validated'
    assert good['lines'][0]['probability']==.9
    bad=align([{**w,'probability':.1} for w in words])
    assert bad['coverage']==0 and bad['lrc'] is None
    def forbidden(*args):raise AssertionError('No independent matching words')
    mismatch=align_with_provider([0]*16000,'синий автобус',words,1,forbidden,unscored_words=True)
    assert mismatch['coverage']==0 and mismatch['lrc'] is None
