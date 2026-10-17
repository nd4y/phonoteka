"""Exercise two real containers, their permissions, and synthetic media.

Run after `docker build -t phonoteka:ci .` on Linux with ffmpeg installed.
No external provider calls or user music are used.
"""
import hashlib
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zlib


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    image = os.environ.get('TEST_IMAGE', 'phonoteka:ci')
    prefix = 'phonoteka-ci-' + uuid.uuid4().hex[:8]
    app, worker = prefix + '-app', prefix + '-worker'
    with tempfile.TemporaryDirectory(prefix=prefix) as folder:
        root = Path(folder)
        for name in ('state','work','original','publication','guard','secrets'):
            (root/name).mkdir()
        password = uuid.uuid4().hex + '-container-test'
        (root/'secrets/password').write_text(password)
        (root/'guard/deployment.json').write_text(json.dumps({
            'storage_domain':prefix, 'instance_id':prefix,
            'work_root':'/work','source_root':'/original','publication_root':'/publication',
        }))
        for ext, codec in (('mp3','libmp3lame'), ('flac','flac')):
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=2','-metadata','title=Fixture original','-metadata','artist=Container test','-c:a',codec,str(root/'original'/('fixture.'+ext))],check=True)
            shutil.copy2(root/'original'/('fixture.'+ext),root/'work'/('fixture.'+ext))
        before = {p.name:sha(p) for p in (root/'original').iterdir()}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        url=f'http://localhost:{port}'
        env={
            'APP_URL':url, 'ALLOW_INSECURE_LOCALHOST':'true',
            'OWNER_USERNAME':'owner','OWNER_PASSWORD_FILE':'/run/secrets/password',
            'STATE_DIR':'/state','WORK_ROOT':'/work','SOURCE_ROOT':'/original',
            'PUBLICATION_ROOT':'/publication','STORAGE_GUARD_DIR':'/storage-guard',
            'READ_ONLY':'false','STORAGE_VERIFIED':'true','INSTANCE_ID':prefix,
            'FILE_WORKERS':'2','IO_THREADS':'2',
        }
        common=['docker','run','-d','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user',f'{os.getuid()}:{os.getgid()}','--tmpfs','/tmp:rw,size=128m,mode=1777']
        for key,value in env.items(): common += ['-e',f'{key}={value}']
        common += ['-v',f'{root}/state:/state','-v',f'{root}/original:/original:ro','-v',f'{root}/secrets/password:/run/secrets/password:ro']
        try:
            command(*common,'--name',app,'-p',f'127.0.0.1:{port}:8000','-v',f'{root}/work:/work:ro',image)
            command(*common,'--name',worker,'-v',f'{root}/work:/work','-v',f'{root}/publication:/publication','-v',f'{root}/guard:/storage-guard',image,'python','-m','phonoteka.worker')
            jar=http.cookiejar.CookieJar()
            client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            csrf=''
            def request(path,method='GET',body=None,headers=None):
                h={'Origin':url,'Content-Type':'application/json','X-CSRF-Token':csrf}
                h.update(headers or {})
                req=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers=h)
                with client.open(req,timeout=15) as response:
                    data=response.read()
                    return json.loads(data) if response.headers.get('Content-Type','').startswith('application/json') else data
            for _ in range(80):
                try:
                    request('/health');break
                except (OSError,urllib.error.URLError):time.sleep(.25)
            else:raise AssertionError('App did not become healthy')
            try:request('/api/tracks')
            except urllib.error.HTTPError as error:assert error.code==401
            else:raise AssertionError('Private library accessible without authentication')
            request('/api/auth/login','POST',{'username':'owner','password':password})
            session=request('/api/auth/session')
            assert session['authenticated']
            csrf=session['csrf_token']
            assert b'<html' in request('/').lower(), 'Production frontend not served'
            def job(kind,ids=None,params=None):
                body={'kind':kind,'track_ids':ids or [],'params':params or {}}
                key=str(uuid.uuid4())
                result=request('/api/jobs','POST',body,{'Idempotency-Key':key})
                duplicate=request('/api/jobs','POST',body,{'Idempotency-Key':key})
                jid=result.get('id') or result['job']['id']
                assert (duplicate.get('id') or duplicate['job']['id'])==jid
                for _ in range(160):
                    current=request('/api/jobs/'+jid)
                    state=current.get('state',current.get('status'))
                    if state in ('succeeded','completed','failed','cancelled','conflict','partial','interrupted'):
                        assert state in ('succeeded','completed'),current
                        return current
                    time.sleep(.25)
                raise AssertionError(f'Timeout {kind}: {current}')
            job('scan')
            tracks=request('/api/tracks')['items']
            assert len(tracks)==2,tracks
            target=next(t for t in tracks if t['relative_path'].endswith('.flac'))
            tid=target['id']
            detail=request('/api/tracks/'+tid)
            stale='0'*64
            try:request('/api/tracks/'+tid+'/proposal','POST',{'tags':{'title':'Changed'}},{'If-Match':stale})
            except urllib.error.HTTPError as error:assert error.code==412
            else:raise AssertionError('Stale proposal accepted')
            def chunk(kind,data):
                return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
            png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\xf0\x80\x40'))+chunk(b'IEND',b'')
            cover='data:image/png;base64,'+base64.b64encode(png).decode()
            request('/api/tracks/'+tid+'/proposal','POST',{'tags':{'title':'Verified fixture','album':'Synthetic fixtures'},'lrc':'[00:00.000]First line\n[00:01.000]Second line','cover':cover},{'If-Match':detail['sha256']})
            job('apply',[tid])
            changed=request('/api/tracks/'+tid)
            assert changed['sha256']!=detail['sha256']
            assert 'Verified fixture' in str(changed['tags']['title'])
            assert request('/api/tracks/'+tid+'/art')==png
            job('audit',[tid])
            job('publish',[tid])
            published=root/'publication/Ready/fixture.flac'
            assert published.is_file(),list((root/'publication').rglob('*'))
            assert sha(published)==sha(root/'work/fixture.flac')
            job('rollback',[tid])
            assert sha(root/'work/fixture.flac')==before['fixture.flac']
            assert {p.name:sha(p) for p in (root/'original').iterdir()}==before
            inspect=json.loads(command('docker','inspect',app,worker))
            assert all(x['HostConfig']['ReadonlyRootfs'] for x in inspect)
            assert all(not x['HostConfig']['Privileged'] and x['Config']['User']!='0' for x in inspect)
            assert not next(m for m in inspect[0]['Mounts'] if m['Destination']=='/work')['RW']
            assert all(not next(m for m in x['Mounts'] if m['Destination']=='/original')['RW'] for x in inspect)
            request('/api/auth/logout','POST',{})
            assert not request('/api/auth/session')['authenticated']
            print('PASS: auth, CSRF session, production UI, scan, job dedup, stale guard, tags+LRC apply, audit, Ready, rollback, original hashes, container isolation')
        except Exception:
            for name in (app,worker):
                subprocess.run(['docker','logs','--tail','35',name],check=False)
            raise
        finally:
            subprocess.run(['docker','rm','-f',app,worker],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)


if __name__=='__main__':
    main()
