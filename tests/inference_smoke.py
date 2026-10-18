"""Real CPU model inference on generated speech, never a fixture response."""
import hashlib,json,pathlib,subprocess,tempfile,time,urllib.request
with tempfile.TemporaryDirectory() as directory:
    root=pathlib.Path(directory);(root/'work').mkdir();(root/'state').mkdir()
    (root/'key').write_text('test-inference-key-not-production')
    (root/'metrics-key').write_text('test-metrics-key-distinct-from-inference')
    subprocess.run(['espeak','-w',str(root/'work'/'speech.wav'),'This is a music library test. We are checking the words and their timing.'],check=True)
    subprocess.run(['chmod','-R','a+rwx',str(root)],check=True)
    name='phonoteka-inference-test'
    subprocess.run(['docker','run','-d','--name',name,'--user','10001:10001','--memory','3g','--cpus','2','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=128m','-p','127.0.0.1:18001:8001','-v',str(root/'work')+':/work:ro','-v',str(root/'state')+':/inference:rw','-v',str(root/'key')+':/run/secrets/inference_key:ro','-v',str(root/'metrics-key')+':/run/secrets/metrics_token:ro','-e','METRICS_TOKEN_FILE=/run/secrets/metrics_token','phonoteka-inference:ci'],check=True)
    def request(path,body=None):
        req=urllib.request.Request('http://127.0.0.1:18001'+path,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json','Authorization':'Bearer test-inference-key-not-production'})
        with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
    try:
        for _ in range(45):
            try: request('/health');break
            except Exception:time.sleep(1)
        body={'relative_path':'speech.wav','sha256':hashlib.sha256((root/'work'/'speech.wav').read_bytes()).hexdigest()}
        health=request('/health');assert health['contract']=='transcribe-v3' and health['resource_profile']['resident_models']==1
        first=request('/jobs',body);second=request('/jobs',body);assert first['id']==second['id']
        for _ in range(180):
            status=request('/jobs/'+first['id'])
            if status['state'] in {'failed','succeeded'}:break
            time.sleep(2)
        assert status['state']=='succeeded',status
        result=status['result'];assert 'test' in result['text'].lower(),result
        assert len(result['words'])>5 and all(0<=w['start']<=w['end']<=result['duration']+.1 for w in result['words'])
        assert result['automatic_acceptance'] is False
        assert result['resource_profile']==health['profile_revision']
        alignment_body={**body,'reference_text':'This is a music library test.\nWe are checking the words and their timing.'}
        aligned_job=request('/jobs',alignment_body)
        assert aligned_job['id']!=first['id']
        for _ in range(180):
            aligned_status=request('/jobs/'+aligned_job['id'])
            if aligned_status['state'] in {'failed','succeeded'}:break
            time.sleep(2)
        assert aligned_status['state']=='succeeded',aligned_status
        alignment=aligned_status['result']['alignment']
        assert alignment['method']=='whisper_attention_dtw_with_independent_asr_anchors_v1'
        assert alignment['coverage']>0,alignment
        assert alignment['automatic_acceptance'] is False
        assert all(line['start_ms'] is None or 0<=line['start_ms']<=line['end_ms']<=result['duration']*1000+100 for line in alignment['lines'])
        unrelated=request('/jobs',{**body,'reference_text':'A submarine landed silently on the distant planet.'})
        for _ in range(180):
            wrong=request('/jobs/'+unrelated['id'])
            if wrong['state'] in {'failed','succeeded'}:break
            time.sleep(2)
        assert wrong['state']=='succeeded',wrong
        assert wrong['result']['alignment']['coverage']==0 and wrong['result']['alignment']['lrc'] is None
        req=urllib.request.Request('http://127.0.0.1:18001/metrics',headers={'Authorization':'Bearer test-metrics-key-distinct-from-inference'})
        with urllib.request.urlopen(req) as response:
            metrics=response.read().decode();assert 'phonoteka_inference_model_loaded 1.0' in metrics and 'phonoteka_collector_up 1.0' in metrics and 'phonoteka_inference_duration_seconds_count 3.0' in metrics
        assert 'phonoteka_inference_model_process_starts_total 1.0' in metrics
        values={line.split()[0]:float(line.split()[1]) for line in metrics.splitlines() if line.startswith('phonoteka_inference_model_process_peak_bytes ')}
        assert 0<values['phonoteka_inference_model_process_peak_bytes']<health['resource_profile']['model_process_rss_bytes']
        assert not list((root/'state').glob('model-*'))
        logs=subprocess.run(['docker','logs',name],capture_output=True,text=True,check=True)
        events=[json.loads(line) for line in logs.stdout.splitlines() if line.strip()]
        assert any(e['event']=='inference_finished' for e in events)
        assert 'speech.wav' not in logs.stdout and 'test-metrics-key' not in logs.stdout and not logs.stderr
        print(json.dumps({'real_model':result['model'],'revision':result['model_revision'],'words':len(result['words']),'timeline_valid':True,'deduplication':True,'forced_alignment_coverage':alignment['coverage']}))
    except BaseException:
        subprocess.run(['docker','logs','--tail','100',name],check=False)
        raise
    finally:subprocess.run(['docker','rm','-f',name],check=False)
