"""Real CPU model inference on generated speech, never a fixture response."""
import hashlib,json,pathlib,subprocess,tempfile,time,urllib.request
with tempfile.TemporaryDirectory() as directory:
    root=pathlib.Path(directory);(root/'work').mkdir();(root/'state').mkdir()
    (root/'key').write_text('test-inference-key-not-production')
    subprocess.run(['espeak','-w',str(root/'work'/'speech.wav'),'This is a music library test. We are checking the words and their timing.'],check=True)
    subprocess.run(['chmod','-R','a+rwx',str(root)],check=True)
    name='phonoteka-inference-test'
    subprocess.run(['docker','run','-d','--name',name,'--user','10001:10001','--memory','3g','--cpus','2','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=128m','-p','127.0.0.1:18001:8001','-v',str(root/'work')+':/work:ro','-v',str(root/'state')+':/inference:rw','-v',str(root/'key')+':/run/secrets/inference_key:ro','phonoteka-inference:ci'],check=True)
    def request(path,body=None):
        req=urllib.request.Request('http://127.0.0.1:18001'+path,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json','Authorization':'Bearer test-inference-key-not-production'})
        with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
    try:
        for _ in range(45):
            try: request('/health');break
            except Exception:time.sleep(1)
        body={'relative_path':'speech.wav','sha256':hashlib.sha256((root/'work'/'speech.wav').read_bytes()).hexdigest()}
        first=request('/jobs',body);second=request('/jobs',body);assert first['id']==second['id']
        for _ in range(180):
            status=request('/jobs/'+first['id'])
            if status['state'] in {'failed','succeeded'}:break
            time.sleep(2)
        assert status['state']=='succeeded',status
        result=status['result'];assert 'test' in result['text'].lower(),result
        assert len(result['words'])>5 and all(0<=w['start']<=w['end']<=result['duration']+.1 for w in result['words'])
        assert result['automatic_acceptance'] is False
        print(json.dumps({'real_model':result['model'],'revision':result['model_revision'],'words':len(result['words']),'timeline_valid':True,'deduplication':True}))
    finally:subprocess.run(['docker','rm','-f',name],check=False)
