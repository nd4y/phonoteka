import copy,hashlib,io,json,subprocess,sys,wave
import httpx
import pytest
from phonoteka import media,remote_asr
from phonoteka.forced_alignment import align_with_provider

def pcm():
    stream=io.BytesIO()
    with wave.open(stream,'wb') as w:w.setparams((1,2,16000,0,'NONE','NONE'));w.writeframes(b'\0\0'*16000)
    return stream.getvalue()

@pytest.fixture
def setup(tmp_path):
    key=tmp_path/'key';key.write_text('k'*40)
    audio=tmp_path/'audio.wav';audio.write_bytes(pcm())
    identity={'model':remote_asr.MODEL,'model_revision':remote_asr.REVISION,'model_sha256':remote_asr.MODEL_SHA256}
    health={**identity,'word_probabilities':True,'capabilities':['transcribe','align_window'],'parallelism':1}
    words=[{'word':'hello','start':.1,'end':.3,'probability':.9},{'word':'world','start':.4,'end':.7,'probability':.95}]
    body={**identity,'input_sha256':hashlib.sha256(pcm()).hexdigest(),'duration':1,'language':'en','words':words,'segments':[{'start':.1,'end':.7,'text':'hello world','avg_logprob':-.1,'no_speech_prob':.01,'words':words}]}
    return key,audio,health,body

def engine(setup,change=None,status=200):
    key,_,health,body=setup
    def handler(request):
        assert request.url.scheme=='https' and request.headers['Authorization']=='Bearer '+'k'*40
        if request.url.path.endswith('/health'):return httpx.Response(200,json=health)
        value=copy.deepcopy(body)
        if change:change(value)
        return httpx.Response(status,json=value)
    return remote_asr.Engine(url='https://engine.test/v1/whisper',keyfile=key,transport=httpx.MockTransport(handler))

def test_pinned_result_audio_identity_and_real_confidence(setup):
    with engine(setup) as provider:
        result=provider.transcribe(setup[1],'en',1)
    assert result['words'][0]['probability']==.9
    assert result['text']=='hello world' and result['device']=='pc2_cuda'
    assert result['automatic_acceptance'] is False

@pytest.mark.parametrize('change,code',[
    (lambda r:r.update(input_sha256='0'*64),'audio_identity'),
    (lambda r:r.update(model_revision='unknown'),'model_identity'),
    (lambda r:r.update(duration=2),'duration'),
    (lambda r:r['words'][0].update(probability=None),'word_contract'),
    (lambda r:r['words'][0].update(probability=True),'word_contract'),
    (lambda r:r['words'][0].update(start=float('inf')),'word_contract'),
    (lambda r:r['words'][0].update(end=2),'word_timeline'),
    (lambda r:r.update(language='ru'),'language'),
])
def test_untrusted_results_cannot_bypass_local_gates(setup,change,code):
    # json encoder does not permit infinity: exercise that validator directly.
    if code=='word_contract' and 'inf' in repr(change.__code__.co_consts):
        value=copy.deepcopy(setup[3]);change(value)
        with pytest.raises(media.MediaError,match='word contract'):remote_asr.validate_words(value['words'],1)
        return
    with engine(setup,change) as provider,pytest.raises(media.MediaError) as caught:provider.transcribe(setup[1],'en',1)
    assert caught.value.code=='inference_pc2_'+code

def test_model_unavailable_has_no_silent_local_fallback(setup):
    with engine(setup,status=503) as provider,pytest.raises(media.MediaError) as caught:provider.transcribe(setup[1],None,1)
    assert caught.value.code=='inference_pc2_unavailable'

def test_plain_or_redirecting_endpoint_is_refused(setup):
    with pytest.raises(media.MediaError):remote_asr.Engine(url='http://engine.test/v1/whisper',keyfile=setup[0])
    with pytest.raises(media.MediaError):remote_asr.Engine(url='https://user:password@engine.test/v1/whisper',keyfile=setup[0])

def test_result_size_is_bounded_before_json(setup):
    key,_,health,_=setup
    def handler(request):return httpx.Response(200,json=health) if request.url.path.endswith('/health') else httpx.Response(200,content=b'x'*(remote_asr.MAX_RESULT+1))
    with remote_asr.Engine(url='https://engine.test/v1/whisper',keyfile=key,transport=httpx.MockTransport(handler)) as provider,pytest.raises(media.MediaError) as caught:provider.transcribe(setup[1],'en',1)
    assert caught.value.code=='inference_pc2_result_budget'

def test_bad_reference_never_reaches_alignment_model():
    words=[{'word':'hello','start':.1,'end':.3,'probability':.9},{'word':'world','start':.4,'end':.7,'probability':.95}]
    def forbidden(*args):raise AssertionError('No independent anchor')
    result=align_with_provider([0]*16000,'A submarine landed',words,1,forbidden)
    assert result['coverage']==0 and result['lrc'] is None

def test_remote_windows_keep_confidence_and_anchor_gates():
    words=[{'word':'hello','start':.1,'end':.3,'probability':.9},{'word':'world','start':.4,'end':.7,'probability':.95}]
    result=align_with_provider([0]*16000,'hello world',words,1,lambda clip,text:copy.deepcopy(words))
    assert result['coverage']==1 and result['automatic_acceptance'] is False
    bad=copy.deepcopy(words);bad[0]['probability']=.01;bad[1]['probability']=.02
    result=align_with_provider([0]*16000,'hello world',words,1,lambda clip,text:bad)
    assert result['coverage']==0 and result['lrc'] is None

def test_remote_health_registry_does_not_claim_nas_resident_weights():
    import os
    env={**os.environ,'ASR_BACKEND':'pc2','ASR_MODEL':remote_asr.MODEL}
    r=subprocess.run([sys.executable,'-c','import json;from phonoteka.inference import health; print(json.dumps(health()))'],env=env,capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
    value=json.loads(r.stdout);assert value['model']==remote_asr.MODEL
    assert value['resource_profile']['resident_models']==0 and value['resource_profile']['execution_device']=='pc2_cuda'
