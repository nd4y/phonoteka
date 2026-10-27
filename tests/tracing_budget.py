"""Generated audio + real HTTP/DNS and mTLS OTLP; never collection data.

Run on build-01 / CI: python tests/tracing_budget.py --output RESULT.json
The optional ten-minute outage probe belongs on an isolated CI runner.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import resource
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave


def fixtures(root, audio=True):
    def openssl(*args): subprocess.run(['openssl',*args],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    openssl('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-days','1',
        '-subj','/CN=Generated Trace Test CA','-keyout',str(root/'ca.key'),'-out',str(root/'ca.pem'))
    for name,cn,ext in [('server','localhost','subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n'),('client','generated-trace-test','extendedKeyUsage=clientAuth\n')]:
        (root/(name+'.ext')).write_text(ext)
        openssl('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-subj','/CN='+cn,
            '-keyout',str(root/(name+'.key')),'-out',str(root/(name+'.csr')))
        openssl('x509','-req','-in',str(root/(name+'.csr')),'-CA',str(root/'ca.pem'),'-CAkey',str(root/'ca.key'),
            '-CAcreateserial','-days','1','-extfile',str(root/(name+'.ext')),'-out',str(root/(name+'.crt')))
    (root/'token').write_text('generated-trace-test-token-not-production-20261027')
    if not audio: return
    with wave.open(str(root/'generated.wav'),'wb') as output:
        output.setparams((1,2,16000,0,'NONE','not compressed'))
        output.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(2*math.pi*440*n/16000))) for n in range(16000)))
    original=(root/'generated.wav').read_bytes()
    for index in range(500):
        # Different valid PCM revision for every generated file, same decode cost.
        (root/(str(index)+'.wav')).write_bytes(original[:44]+struct.pack('<h',index)+original[46:])


def percentile(values): return sorted(values)[math.ceil(len(values)*.95)-1]


def child(root, enabled, count, outage_seconds, offset):
    import httpx
    from phonoteka import tracing
    class Provider(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(.03);self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
        def log_message(self,*args): pass
    class Receiver(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        outage=False
        spans=0
        def do_POST(self):
            data=self.rfile.read(int(self.headers.get('Content-Length',0)))
            if len(data)>512*1024:raise RuntimeError('export_size_budget')
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
            value=ExportTraceServiceRequest.FromString(data)
            Receiver.spans+=sum(len(scope.spans) for r in value.resource_spans for scope in r.scope_spans)
            self.send_response(503 if Receiver.outage else 200);self.send_header('Content-Length','0');self.end_headers()
        def log_message(self,*args): pass
    provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(root/'server.crt',root/'server.key')
    ctx.load_verify_locations(root/'ca.pem');ctx.verify_mode=ssl.CERT_REQUIRED
    receiver.socket=ctx.wrap_socket(receiver.socket,server_side=True)
    for server in (provider,receiver):threading.Thread(target=server.serve_forever,daemon=True).start()
    start_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    if enabled:
        os.environ.update(PHONOTEKA_TRACING_ENABLED='true',OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=f'https://localhost:{receiver.server_port}/v1/traces',
            OTEL_EXPORTER_OTLP_CERTIFICATE=str(root/'ca.pem'),OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE=str(root/'client.crt'),
            OTEL_EXPORTER_OTLP_CLIENT_KEY=str(root/'client.key'),OTEL_EXPORTER_OTLP_TOKEN_FILE=str(root/'token'),OTEL_HOST_NAME='generated-acceptance')
        tracing.configure('worker');assert tracing.diagnostics()['state']=='enabled'
    durations=[]
    async def run():
        sem=asyncio.Semaphore(4)
        async with httpx.AsyncClient(trust_env=False,limits=httpx.Limits(max_connections=4)) as client:
            async def one(index):
                async with sem:
                    started=time.perf_counter()
                    with tracing.file_attempt(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())):
                        with tracing.span('audio.decode',tracing.attributes(stage='decode')):
                            await asyncio.to_thread(subprocess.run,['ffmpeg','-nostdin','-v','error','-threads','1','-i',str(root/(str(offset+index)+'.wav')),'-f','null','-'],
                                check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                        with tracing.span('provider.lrclib',tracing.attributes(stage='metadata_lookup',provider='lrclib')):
                            response=await client.get(f'http://localhost:{provider.server_port}/generated');assert response.content==b'ok'
                    durations.append(time.perf_counter()-started)
            await asyncio.gather(*(one(n) for n in range(count)))
    started=time.perf_counter();asyncio.run(run());elapsed=time.perf_counter()-started
    if enabled: assert tracing._processor.force_flush(2000)
    result={'enabled':enabled,'files':count,'workers':4,'wall_seconds':elapsed,'p95_seconds':percentile(durations),
        'max_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'rss_before_enable_bytes':start_rss,
        'received_spans':Receiver.spans,'export':tracing.diagnostics()}
    if enabled:
        assert result['export']['failed']==result['export']['dropped']==result['export']['partial']==0
        assert result['export']['accepted']==result['export']['exported']==Receiver.spans
    if enabled and outage_seconds:
        Receiver.outage=True;deadline=time.monotonic()+outage_seconds;completed=0;peak=0
        while time.monotonic()<deadline:
            with tracing.span('file.stage',tracing.attributes(stage='preflight')):completed+=1
            peak=max(peak,tracing.diagnostics()['queued_bytes']);time.sleep(.05)
        assert tracing._processor.force_flush(2000)
        assert tracing.diagnostics()['failed']>0 and peak<=8*1024*1024
        Receiver.outage=False
        with tracing.span('file.stage',tracing.attributes(stage='preflight')):pass
        assert tracing._processor.force_flush(2000)
        before=time.monotonic();tracing.shutdown();shutdown=time.monotonic()-before
        assert shutdown<=2.1
        result['outage']={'seconds':outage_seconds,'work_completed':completed,'queue_peak_bytes':peak,'shutdown_seconds':shutdown,'export_after':tracing.diagnostics()}
    else:tracing.shutdown()
    print(json.dumps(result),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);parser.add_argument('--child',choices=['on','off'])
    parser.add_argument('--root',type=Path);parser.add_argument('--count',type=int,default=125);parser.add_argument('--outage-seconds',type=int,default=0)
    parser.add_argument('--offset',type=int,default=0)
    args=parser.parse_args()
    if args.child:return child(args.root,args.child=='on',args.count,args.outage_seconds,args.offset)
    assert args.output is not None
    samples=[]
    with tempfile.TemporaryDirectory(prefix='phonoteka-trace-budget-') as directory:
        root=Path(directory);fixtures(root)
        # Alternate modes to reduce bias from shared-host load, caches and temperature.
        counts={False:0,True:0}
        for index,enabled in enumerate((False,True,True,False,False,True,True,False)):
            command=[sys.executable,__file__,'--child','on' if enabled else 'off','--root',str(root),'--count',str(args.count),'--offset',str(counts[enabled]*args.count)]
            counts[enabled]+=1
            if index==6 and args.outage_seconds:command+=['--outage-seconds',str(args.outage_seconds)]
            process=subprocess.run(command,capture_output=True,text=True,timeout=1200)
            if process.returncode:
                # Only generated fixtures are used here. Preserve the actual failed
                # assertion instead of losing child diagnostics behind run(check=True).
                print(process.stderr[-6000:],file=sys.stderr)
                raise RuntimeError('generated_workload_child_failed')
            samples.append(json.loads(process.stdout))
    off=[s for s in samples if not s['enabled']];on=[s for s in samples if s['enabled']]
    import statistics
    baseline=statistics.median(s['p95_seconds'] for s in off);measured=statistics.median(s['p95_seconds'] for s in on)
    wall_off=sum(s['wall_seconds'] for s in off);wall_on=sum(s['wall_seconds'] for s in on)
    result={'files_per_mode':sum(s['files'] for s in on),'workers':4,'p95_overhead':measured/baseline-1,
        'throughput_loss':max(0,1-wall_off/wall_on),'additional_rss_bytes':max(s['max_rss_bytes'] for s in on)-max(s['max_rss_bytes'] for s in off),'samples':samples}
    result['passed']=result['p95_overhead']<=.05 and result['throughput_loss']<=.05 and result['additional_rss_bytes']<=32*1024*1024
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='samples'}))
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
