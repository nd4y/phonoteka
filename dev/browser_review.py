"""Build-01-only isolated browser acceptance fixtures, using normal owner auth.

This disposable account contains generated silence/CUE only. Its published test
password must never be reused for private data. The ordinary demo is untouched.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
from xml.etree import ElementTree

ROOT=Path('/home/av/build/phonoteka')
LABEL='io.phonoteka.browser-review'
FIXTURE_PASSWORD='synthetic-browser-review-fixture-only'


def command(arguments):
    return subprocess.check_output(arguments,text=True).strip()


def inspect(name):
    result=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
    if result.returncode:return None
    return json.loads(result.stdout)[0]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop'])
    parser.add_argument('snapshot')
    args=parser.parse_args()
    if not re.fullmatch('[0-9a-f]{64}',args.snapshot):parser.error('Full accepted source SHA256 is required')
    snapshot=args.snapshot
    with (ROOT/'operation.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        names=['phonoteka-review-'+role+'-'+snapshot[:12] for role in ('app','worker')]
        if args.action=='stop':
            for name in reversed(names):
                row=inspect(name)
                if not row:continue
                assert row['Config'].get('Labels',{}).get(LABEL)==snapshot,'Container ownership mismatch'
                subprocess.run(['docker','stop','--time','30',row['Id']],check=True,stdout=subprocess.DEVNULL)
                subprocess.run(['docker','rm',row['Id']],check=True,stdout=subprocess.DEVNULL)
            print(json.dumps({'stopped':names,'ordinary_demo_changed':False}));return
        reports=[]
        for path in (ROOT/'reports'/snapshot).glob('*/backend.xml'):
            suites=ElementTree.parse(path).getroot().findall('testsuite')
            if suites and sum(int(item.get('tests',0)) for item in suites)>0 and not any(int(item.get('errors',0)) or int(item.get('failures',0)) for item in suites):reports.append(str(path))
        assert reports,'A passing immutable full backend report is required'
        image=inspect('phonoteka-build01:demo-'+snapshot[:12])
        assert image and image['Config'].get('Labels',{}).get('io.phonoteka.source_sha256')==snapshot
        # Reuse only these generated fixtures between accepted image upgrades,
        # allowing the ordinary service-worker update flow to be exercised.
        base=ROOT/'browser-review'/'fixtures'
        base.mkdir(parents=True,exist_ok=True,mode=0o700)
        credential=base/'owner_password'
        if credential.exists():assert credential.read_text()==FIXTURE_PASSWORD
        else:credential.write_text(FIXTURE_PASSWORD);credential.chmod(0o600)
        common=['--label',LABEL+'='+snapshot,'--label','io.phonoteka.dev=build01',
                '--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cpus','1','--memory','512m',
                '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128',
                '--tmpfs','/tmp:size=128m,mode=1777','--log-opt','max-size=5m','--log-opt','max-file=2',
                '-v',str(base)+':/demo']
        for index,name in enumerate(names):
            existing=inspect(name)
            if existing:
                assert existing['Config'].get('Labels',{}).get(LABEL)==snapshot and existing['Image']==image['Id']
                if not existing['State']['Running']:command(['docker','start',existing['Id']])
                continue
            if index==0:
                command(['docker','run','-d','--name',name,*common,'-p','127.0.0.1:8178:8000',
                         '-e','DEMO_URL=http://localhost:8178',image['Id']])
                for _ in range(120):
                    try:
                        with urllib.request.urlopen('http://localhost:8178/health',timeout=2) as response:
                            if response.status==200:break
                    except Exception:time.sleep(1)
                else:raise RuntimeError('Review app did not become ready; original demo was not changed')
            else:
                environment={'APP_URL':'http://localhost:8178','ALLOW_INSECURE_LOCALHOST':'true',
                             'STATE_DIR':'/demo/state','WORK_ROOT':'/demo/work','SOURCE_ROOT':'/demo/original',
                             'PUBLICATION_ROOT':'/demo/publication','STORAGE_GUARD_DIR':'/demo/guard',
                             'OWNER_PASSWORD_FILE':'/demo/owner_password','READ_ONLY':'false',
                             'STORAGE_VERIFIED':'true','INSTANCE_ID':'build01-synthetic-demo',
                             'FILE_WORKERS':'2','IO_THREADS':'2','EXTERNAL_METADATA':'false',
                             'EXTERNAL_FINGERPRINTS':'false','YOUTUBE_ENABLED':'false'}
                options=[option for key,value in environment.items() for option in ('-e',key+'='+value)]
                command(['docker','run','-d','--no-healthcheck','--name',name,*common,'--network','none',
                         *options,'--entrypoint','python',image['Id'],'-m','phonoteka.worker'])
        result={'snapshot':snapshot,'image_id':image['Id'],'containers':names,'url':'http://localhost:8178',
                'ordinary_demo_changed':False,'verified_backend_reports':reports}
        (base/('review-'+snapshot+'.json')).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))


if __name__=='__main__':main()
