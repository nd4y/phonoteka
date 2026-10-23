"""Real CPU model/resource checks, generated speech, no external network.

Run in the verified inference image, with /scratch tmpfs and /reports RW. The
small VM cgroup cap and shortened watchdog/RSS diagnostics are explicit; this
does not claim a musical pilot, 30-minute audio, GPU or default 6600s elapsed run.
"""
import hashlib,importlib.util,json,os,pathlib,secrets,subprocess,sys,threading,time,urllib.request
from contextlib import suppress

ROOT=pathlib.Path('/scratch');STATE=ROOT/'inference';WORK=ROOT/'work';REPORTS=pathlib.Path('/reports')
TEXT='This is a music library test. We are checking the words and their timing.'

def main():
 from phonoteka.inference_budget import ModelProcess,BudgetError,MAX_PCM_BYTES,MAX_RSS_BYTES,MAX_JOB_SECONDS
 for path in (ROOT,STATE,WORK,REPORTS):path.mkdir(parents=True,exist_ok=True)
 key=secrets.token_urlsafe(32);metrics_key=secrets.token_urlsafe(32)
 (ROOT/'key').write_text(key);(ROOT/'metrics-key').write_text(metrics_key)
 spec=importlib.util.spec_from_file_location('acceptance_perf','/performance_acceptance.py');perf=importlib.util.module_from_spec(spec);spec.loader.exec_module(perf)
 report={'checks':{},'phases':[],'resources':[],
  'acceptance_resource_profile':{'id':'generated_asr19_ordinary7_memory3840_v2','revision':os.environ['ACCEPTANCE_REVISION'],'configured_limits':{'inference_memory': 3221225472, 'ordinary_memory': 268435456, 'reserved_host_memory': 536870912, 'stop_below_available': 536870912, 'inference_cpus': 2, 'ordinary_cpus': 1, 'overall_deadline_seconds': 600}},
  'limitations':[
  'Generated clear English speech is not musical or Poweramp acceptance.',
  'Separate ordinary Worker/API container; same-Worker inference polling isolation remains a contract-test result.',
  'Declared CPU 3 GiB inference profile; undersized1280MiB refusal is a separate real-container smoke check.',
  'Short actual-model deadline/RSS diagnostics prove cleanup mechanism; not 6600 seconds elapsed or kernel OOM.']}
 stop=threading.Event();server=None;phase='fixture_generation';monitor=None
 def require(name,value):
  report['checks'][name]=bool(value)
  if not value:raise AssertionError(name)
 def sample():
  while not stop.wait(.25):
   row=perf.process_sample();sizes=[];pcm=[]
   for path in STATE.glob('model-*/**/*'):
    try:
     if path.is_file():
      size=path.stat().st_size;sizes.append(size)
      if path.name=='analysis.wav':pcm.append(size)
    except OSError:pass  # The supervised owner may finish/remove this job.
   row['pcm_bytes']=max(pcm,default=0);row['scratch_bytes']=sum(sizes)
   report['resources'].append(row)
 def request(path,body=None,metrics=False):
  req=urllib.request.Request('http://127.0.0.1:8001'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json','Authorization':'Bearer '+(metrics_key if metrics else key)})
  with urllib.request.urlopen(req,timeout=8) as response:return response.read().decode() if metrics else json.load(response)
 def start_server():
  proc=subprocess.Popen([sys.executable,'-m','phonoteka.serve','inference'],stdout=log,stderr=subprocess.STDOUT)
  deadline=time.monotonic()+30
  while time.monotonic()<deadline:
   if proc.poll() is not None:raise RuntimeError('inference_server_exited')
   try:health=request('/health');return proc,health
   except Exception:time.sleep(.25)
  proc.terminate();proc.wait(timeout=15);raise TimeoutError('inference_startup_deadline')
 def finish_server(proc):
  proc.terminate()
  try:proc.wait(timeout=20)
  except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5);raise
 def wait_job(body,timeout=90):
  job=request('/jobs',body);require('remote_dedup_'+str(len(report['phases'])),request('/jobs',body)['id']==job['id'])
  started=time.monotonic();deadline=started+timeout
  while time.monotonic()<deadline:
   result=request('/jobs/'+job['id'])
   if result['state'] in {'succeeded','failed','interrupted','cancelled'}:
    report['phases'].append({'kind':phase,'start':started,'end':time.monotonic(),'state':result['state'],'code':(result.get('result') or {}).get('code')});return result
   time.sleep(.25)
  raise TimeoutError('remote_job_deadline')
 with (REPORTS/'inference-service.log').open('w') as log:
  try:
   subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',"flite=text='"+TEXT+"':voice=slt",'-ar','16000',str(WORK/'speech.wav')],check=True,timeout=30)
   subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','anullsrc=r=16000:cl=mono','-t','1801','-c:a','flac',str(WORK/'too-long.flac')],check=True,timeout=30)
   body={'relative_path':'speech.wav','sha256':hashlib.sha256((WORK/'speech.wav').read_bytes()).hexdigest(),'language':'en'}
   monitor=threading.Thread(target=sample,daemon=True);monitor.start();server,health=start_server()
   report['advertised_profile']=health['resource_profile']
   require('pinned_cpu_contract',health['contract']=='transcribe-v3' and health['parallelism']==1 and health['resource_profile']['resident_models']==1 and not health['resource_profile']['gpu'])
   phase='actual_asr';asr=wait_job(body);require('actual_asr_succeeded',asr['state']=='succeeded')
   result=asr['result'];require('actual_words_and_original_timeline','test' in result['text'].lower() and len(result['words'])>5 and all(0<=w['start']<=w['end']<=result['duration']+.1 for w in result['words']) and result['automatic_acceptance'] is False)
   phase='actual_alignment';aligned=wait_job({**body,'reference_text':TEXT.replace('. We','.\nWe')});require('actual_alignment_succeeded',aligned['state']=='succeeded' and aligned['result']['alignment']['coverage']>0 and not aligned['result']['alignment']['automatic_acceptance'])
   phase='negative_text';wrong=wait_job({**body,'reference_text':'A submarine landed silently on the distant planet.'});require('wrong_text_rejected',wrong['state']=='succeeded' and wrong['result']['alignment']['coverage']==0 and wrong['result']['alignment']['lrc'] is None)
   metrics=request('/metrics',metrics=True);require('one_resident_model_for_asr_and_align','phonoteka_inference_model_process_starts_total 1.0' in metrics)
   phase='long_compressed_input';long=wait_job({'relative_path':'too-long.flac','sha256':hashlib.sha256((WORK/'too-long.flac').read_bytes()).hexdigest()},timeout=30)
   require('over_duration_refused',long['state']=='failed' and long['result']['code']=='inference_duration_budget')
   finish_server(server);server=None;offline_started=time.monotonic()
   phase='actual_loaded_model_short_deadline';worker=ModelProcess(scratch_root=STATE)
   try:
    warm=worker.run(body);require('diagnostic_uses_actual_loaded_model','test' in warm['text'].lower())
    old_pid=worker.process.pid;worker.deadline=.01;started=time.monotonic()
    try:worker.run({**body,'reference_text':TEXT})
    except BudgetError as exc:code=exc.code
    else:code='unexpected_success'
    report['phases'].append({'kind':phase,'start':started,'end':time.monotonic(),'code':code,'diagnostic_deadline_seconds':.01})
    require('actual_deadline_unloads_child',code=='inference_execution_timeout' and worker.process is None and not pathlib.Path('/proc',str(old_pid)).exists() and not list(STATE.glob('model-*')))
   finally:worker.close()
   phase='actual_model_rss_admission_diagnostic';worker=ModelProcess(scratch_root=STATE)
   try:
    warm=worker.run(body)
    assert 'test' in warm['text'].lower() and worker.process is not None and worker.peak_rss>128*1024**2
    old_pid=worker.process.pid;worker.max_rss=128*1024**2;worker.deadline=30
    started=time.monotonic()
    try:worker.run(body)
    except BudgetError as exc:code=exc.code
    else:code='unexpected_success'
    report['phases'].append({'kind':phase,'start':started,'end':time.monotonic(),'code':code,'diagnostic_rss_limit':128*1024**2,'peak_rss':worker.peak_rss})
    require('rss_budget_unloads_real_child',code=='inference_memory_budget' and worker.process is None and worker.starts==1 and not pathlib.Path('/proc',str(old_pid)).exists() and not list(STATE.glob('model-*')))
   finally:worker.close()
   report['inference_api_offline_window']={'start':offline_started,'end':time.monotonic()}
   phase='default_service_restart';server,_=start_server();again=wait_job({**body,'generation':1});require('explicit_restart_actual_asr',again['state']=='succeeded' and 'test' in again['result']['text'].lower())
   require('resources_sampled',len(report['resources'])>=10)
   require('pcm_and_temp_bounded',max((x['pcm_bytes'] for x in report['resources']),default=0)<=MAX_PCM_BYTES and max((x['scratch_bytes'] for x in report['resources']),default=0)<=MAX_PCM_BYTES+10*1024**2)
   report['production_default_budgets']={'model_rss':MAX_RSS_BYTES,'deadline_seconds':MAX_JOB_SECONDS}
  except Exception as exc:report['error_type']=type(exc).__name__;report['checks']['protocol_completed']=False
  finally:
   if server is not None:
    try:finish_server(server)
    except Exception as exc:report['cleanup_error_type']=type(exc).__name__;report['checks']['clean_service_shutdown']=False
   stop.set()
   if monitor is not None:monitor.join(timeout=3)
   report['checks']['scratch_clean_after_shutdown']=not list(STATE.glob('model-*'))
 report.update(phase=phase,passed=bool(report['checks']) and all(report['checks'].values()))
 (REPORTS/'inference-result.json').write_text(json.dumps(report,indent=2));print(json.dumps({'passed':report['passed'],'checks':report['checks'],'error_type':report.get('error_type')}));return 0 if report['passed'] else 1

if __name__=='__main__':sys.exit(main())
