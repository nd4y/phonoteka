"""Validate pinned Collector, Jaeger and non-root nginx with generated PKI only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from tracing_budget import fixtures


def run(*args):
    subprocess.run(args,check=True,stdout=subprocess.DEVNULL)


with tempfile.TemporaryDirectory(prefix='phonoteka-trace-infra-') as directory:
    root=Path(directory);fixtures(root,audio=False)
    secret=root/'secrets';secret.mkdir()
    for source,target in [('server.crt','server.crt'),('server.key','server.key'),('ca.pem','ca.pem'),('token','otlp_token')]:
        (secret/target).write_bytes((root/source).read_bytes());(secret/target).chmod(0o644)
    for name in ('queue','jaeger-v220','config'):(root/name).mkdir(mode=0o777)
    for name in ('collector.yaml','jaeger.yaml','nginx.conf'):
        (root/'config'/name).write_bytes((Path('monitoring/tracing')/name).read_bytes())
    env={**os.environ,'TRACE_ROOT':str(root)}
    config=json.loads(subprocess.check_output(['docker','compose','-f','monitoring/tracing/compose.yaml','config','--format','json'],env=env))
    results=[]
    for name,arguments in [('collector',['validate','--config=/etc/otelcol/config.yaml']),('jaeger',['validate','--config=/etc/jaeger/config.yaml']),('ingest',['nginx','-t','-c','/etc/nginx/nginx.conf'])]:
        service=config['services'][name];image=service['image']
        assert '@sha256:' in image, 'Infrastructure image must be immutable'
        run('docker','pull',image)
        command=['docker','run','--rm','--memory=384m','--cpus=1','--user','1026:100','--read-only']
        for mount in service['volumes']:
            command+=['--volume',mount['source']+':'+mount['target']+(':ro' if mount.get('read_only') else '')]
        command+=['--tmpfs','/tmp:rw,size=32m,mode=1777']
        # Syntax validation has no compose DNS; only resolve the upstream here.
        # Real connectivity/auth/persistence are checked against the deployed stack.
        if name=='ingest':command+=['--add-host','collector:127.0.0.1','--entrypoint','nginx',image,'-t','-c','/etc/nginx/nginx.conf']
        else:command+=[image,*arguments]
        run(*command);results.append({'component':name,'image':image,'native_validation':True})
    print(json.dumps({'validated':results}))
