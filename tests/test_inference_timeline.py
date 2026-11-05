"""Decoded duration, remote contract and overflow gates before model calls."""
import subprocess
from types import SimpleNamespace
import wave

import pytest
from phonoteka import inference, media, remote_asr
from phonoteka.inference_budget import BudgetError


def mp3_info(duration):
    return {'duration':duration,'codec':'mp3','container':'mp3',
            'audio_stream_count':1,'channels':2,'sample_rate':44100}


def decoded_pcm(monkeypatch, seconds):
    def process(source, executable, arguments, **kwargs):
        assert executable=='ffmpeg'
        assert float(arguments[arguments.index('-t')+1])>inference.MAX_DURATION
        with wave.open(arguments[-1],'wb') as pcm:
            pcm.setparams((1,2,16000,0,'NONE','NONE'))
            pcm.writeframes(b'\0\0'*int(seconds*16000))
    monkeypatch.setattr(media,'_process',process)


@pytest.mark.parametrize('reported',[1,9])
def test_raw_mp3_uses_decoded_samples_with_duration_evidence(tmp_path,monkeypatch,reported):
    decoded_pcm(monkeypatch,3)
    with inference.analysis_audio(tmp_path/'input.mp3',{'_scratch_root':tmp_path},mp3_info(reported),load_samples=False) as (pcm,samples,evidence):
        assert pcm.exists() and samples is None
        assert evidence=={'source':'decoded_pcm','reported_seconds':reported,'decoded_seconds':3,'header_mismatch':True}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('override',[{'container':'matroska,webm'}, {'codec':'aac'}, {'audio_stream_count':2}])
def test_duration_disagreement_stays_blocked_for_other_timelines(tmp_path,monkeypatch,override):
    decoded_pcm(monkeypatch,3)
    with pytest.raises(BudgetError) as caught:
        with inference.analysis_audio(tmp_path/'input',{'_scratch_root':tmp_path},{**mp3_info(9),**override},load_samples=False):
            pytest.fail('Ambiguous timeline accepted')
    assert caught.value.code=='inference_duration_mismatch'
    assert not list(tmp_path.iterdir())


def test_understated_header_cannot_hide_overflow_with_real_decoder(tmp_path,monkeypatch):
    source=tmp_path/'long.mp3'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=4','-c:a','libmp3lame',str(source)],check=True)
    monkeypatch.setattr(inference,'MAX_DURATION',2)
    monkeypatch.setattr(inference,'MAX_SAMPLES',32000)
    with pytest.raises(BudgetError) as caught:
        with inference.analysis_audio(source,{'_scratch_root':tmp_path},mp3_info(1),load_samples=False):
            pytest.fail('Truncated audio accepted')
    assert caught.value.code=='inference_pcm_budget'
    assert list(tmp_path.iterdir())==[source]


def test_pc2_transcription_and_alignment_share_actual_pcm_duration(tmp_path,monkeypatch):
    from phonoteka import forced_alignment
    decoded_pcm(monkeypatch,3)
    monkeypatch.setattr(inference,'BACKEND','pc2')
    monkeypatch.setattr(inference,'ROOT',tmp_path)
    monkeypatch.setattr(media,'_expect',lambda *args:None)
    monkeypatch.setattr(media,'probe_audio',lambda *args:mp3_info(9))
    (tmp_path/'input.mp3').write_bytes(b'fixture')
    seen=[]
    class Engine:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def transcribe(self,pcm,language,duration):
            with wave.open(str(pcm),'rb') as audio:assert audio.getnframes()/16000==duration
            seen.append(('transcribe',duration))
            return {'words':[],'language':'en'}
    def align(samples,text,words,duration,provider):
        assert len(samples)==48000
        seen.append(('align',duration))
        return {'coverage':0,'lrc':None}
    monkeypatch.setattr(remote_asr,'Engine',Engine)
    monkeypatch.setattr(forced_alignment,'align_with_provider',align)
    result=inference.execute({'relative_path':'input.mp3','sha256':'a'*64,'reference_text':'fixture words','_scratch_root':tmp_path})
    assert seen==[('transcribe',3),('align',3)]
    assert result['duration']==3 and result['duration_evidence']['reported_seconds']==9
    assert result['state']=='draft' and result['alignment']['lrc'] is None


def test_overflow_never_opens_pc2_connection(tmp_path,monkeypatch):
    decoded_pcm(monkeypatch,3)
    monkeypatch.setattr(inference,'MAX_DURATION',2)
    monkeypatch.setattr(inference,'MAX_SAMPLES',32000)
    monkeypatch.setattr(inference,'BACKEND','pc2')
    monkeypatch.setattr(inference,'ROOT',tmp_path)
    monkeypatch.setattr(media,'_expect',lambda *args:None)
    monkeypatch.setattr(media,'probe_audio',lambda *args:mp3_info(1))
    (tmp_path/'input.mp3').write_bytes(b'fixture')
    def forbidden():pytest.fail('PC2 contacted before PCM validation')
    monkeypatch.setattr(remote_asr,'Engine',forbidden)
    with pytest.raises(BudgetError) as caught:
        inference.execute({'relative_path':'input.mp3','sha256':'a'*64,'_scratch_root':tmp_path})
    assert caught.value.code=='inference_pcm_budget'


def test_cpu_alignment_and_result_share_actual_duration(tmp_path,monkeypatch):
    from phonoteka import forced_alignment,inference_budget
    decoded_pcm(monkeypatch,3)
    monkeypatch.setattr(inference,'BACKEND','cpu')
    monkeypatch.setattr(inference,'ROOT',tmp_path)
    monkeypatch.setattr(inference_budget,'require_cpu_capacity',lambda:None)
    monkeypatch.setattr(media,'_expect',lambda *args:None)
    monkeypatch.setattr(media,'probe_audio',lambda *args:mp3_info(9))
    (tmp_path/'input.mp3').write_bytes(b'fixture')
    model=SimpleNamespace(transcribe=lambda samples,**kwargs:([],SimpleNamespace(language='en')))
    monkeypatch.setattr(inference,'MODEL_INSTANCE',model)
    def align(current,samples,text,language,words,duration):
        assert current is model and len(samples)==48000 and duration==3
        return {'coverage':0,'lrc':None}
    monkeypatch.setattr(forced_alignment,'align_reference',align)
    result=inference.execute({'relative_path':'input.mp3','sha256':'a'*64,'reference_text':'fixture words','_scratch_root':tmp_path})
    assert result['duration']==3 and result['duration_evidence']['header_mismatch'] is True
    assert result['automatic_acceptance'] is False
