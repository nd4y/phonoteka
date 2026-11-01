"""Generated ASR-19 and ordinary-work concurrency acceptance, on a Docker host.

No personal media or network inside the two bounded containers. GitHub-hosted
CI is separate evidence from the shared build-01 and musical/phone acceptance.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

MIB = 1024 ** 2
LIMITS = {'inference_memory': 3072*MIB, 'ordinary_memory': 256*MIB,
          'reserved_host_memory': 512*MIB, 'stop_below_available': 512*MIB,
          'inference_cpus': 2, 'ordinary_cpus': 1, 'overall_deadline_seconds': 600}
CHECKS = {'inference_protocol_passed', 'ordinary_measured_checks_passed',
          'ordinary_progress_overlapped_inference', 'ordinary_progress_while_inference_api_offline',
          'ordinary_observation_covers_inference_phases', 'no_host_pressure_or_deadline',
          'containers_completed_cleanly'}
ASR_CHECKS = {'pinned_cpu_contract', 'actual_asr_succeeded', 'actual_words_and_original_timeline',
              'actual_alignment_succeeded', 'wrong_text_rejected', 'one_resident_model_for_asr_and_align',
              'over_duration_refused', 'explicit_restart_actual_asr', 'resources_sampled', 'pcm_and_temp_bounded',
              'diagnostic_uses_actual_loaded_model', 'actual_deadline_unloads_child', 'rss_budget_unloads_real_child',
              'scratch_clean_after_shutdown', *('remote_dedup_'+str(n) for n in (0, 1, 2, 3, 6))}


def available():
    return next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith('MemAvailable:'))


def bounded_json(path):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 8*1024**2:
        raise ValueError('invalid_report')
    return json.loads(path.read_text())


def measured_readiness(value, started, now):
    points = [value.get(k) for k in ('measurement_started_at_monotonic',
              'resource_sample_at_monotonic', 'ready_at_monotonic')]
    return (value.get('state') == 'measurement_sample_observed'
            and all(type(p) in (int, float) and math.isfinite(p) for p in points)
            and started <= points[0] <= points[1] <= points[2] <= now)


def verify_owned(item, cid, name, image, scope, kind):
    """Fail closed before lifecycle operations; names alone never grant ownership."""
    if (not re.fullmatch('[0-9a-f]{64}', cid) or item.get('Id') != cid
            or item.get('Name') != '/'+name or item.get('Image') != image
            or item.get('Config', {}).get('Labels', {}).get('io.phonoteka.acceptance') != scope):
        raise RuntimeError('container_ownership_mismatch')
    host = item['HostConfig']
    if (host['Memory'] != LIMITS[kind+'_memory'] or host['MemorySwap'] != LIMITS[kind+'_memory']
            or host['NanoCpus'] != LIMITS[kind+'_cpus']*10**9 or host['NetworkMode'] != 'none'
            or not host['ReadonlyRootfs'] or host['Privileged'] or host['PidsLimit'] != 256
            or {x.upper() for x in host.get('CapDrop', [])} != {'ALL'} or host.get('CapAdd')
            or not any(x in ('no-new-privileges', 'no-new-privileges:true') for x in host['SecurityOpt'])
            or host.get('PublishAllPorts') or any((host.get('PortBindings') or {}).values())):
        raise RuntimeError('container_bounds_mismatch')
    return item


def combined_checks(ordinary, inference, failure, exit_codes):
    # Same seven predicates as the build-01 memory2048/warm-RSS480 profile.
    phases = inference.get('phases', [])
    progress = ordinary.get('background', {}).get('progress', [])
    overlap = [x for x in progress if any(p['start'] <= x['at'] <= p['end'] for p in phases)]
    window = inference.get('inference_api_offline_window', {})
    offline = [x for x in progress if window.get('start', float('inf')) <= x['at'] <= window.get('end', float('-inf'))]
    samples = ordinary.get('resources', {}).get('samples', [])
    covered = bool(phases and samples) and min(x['at'] for x in samples) <= min(x['start'] for x in phases) and max(x['at'] for x in samples)+2 >= max(x['end'] for x in phases)
    return {'inference_protocol_passed': inference.get('passed') is True,
            'ordinary_measured_checks_passed': ordinary.get('passed_measured_checks') is True,
            'ordinary_progress_overlapped_inference': len(overlap) >= 2,
            'ordinary_progress_while_inference_api_offline': len(offline) >= 1,
            'ordinary_observation_covers_inference_phases': covered,
            'no_host_pressure_or_deadline': failure is None,
            'containers_completed_cleanly': len(exit_codes) == 2 and all(code == 0 for code in exit_codes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-image', required=True)
    parser.add_argument('--inference-image', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.revision):
        raise ValueError('invalid_revision')
    for image in (args.app_image, args.inference_image):
        if not re.fullmatch('sha256:[0-9a-f]{64}', image):
            raise ValueError('immutable_image_id_required')
    root = Path(__file__).resolve().parent
    source = root.parent/'backend/phonoteka'
    out = args.output.resolve()
    out.mkdir(mode=0o700)  # Exclusive run, never reuse another run's evidence.
    for kind in ('ordinary', 'inference'):
        (out/kind).mkdir(mode=0o700)
    scope = 'asr19-'+uuid.uuid4().hex
    images = {'ordinary': args.app_image, 'inference': args.inference_image}
    owned, pressure, cleanup = {}, [], []
    failure = None
    result = {'contract': 'phonoteka-generated-asr19-ordinary7-ci-v1', 'revision': args.revision,
              'scope': scope, 'images': images, 'limits': LIMITS, 'passed': False,
              'limitations': ['GitHub CI does not replace build-01, musical pilot or phone acceptance.',
                              '480-second ordinary lane does not replace the separate 30-minute NFR.'],
              'source_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                  (Path(__file__), root/'inference_resource_acceptance.py', root/'performance_acceptance.py', root/'acceptance_image_probe.py')}}

    def call(argv, timeout=15, check=True):
        return subprocess.run(['docker', *argv], check=check, capture_output=True, text=True, timeout=timeout)

    def inspect(kind):
        entry = owned[kind]
        item = json.loads(call(['inspect', entry['cid']]).stdout)[0]
        return verify_owned(item, entry['cid'], entry['name'], images[kind], scope, kind)

    def guard():
        free = available()
        pressure.append({'at': time.monotonic(), 'available_bytes': free})
        if free < LIMITS['stop_below_available']:
            raise RuntimeError('host_memory_reserve_guard')
        if time.monotonic()-started > LIMITS['overall_deadline_seconds']:
            raise TimeoutError('overall_deadline')

    def launch(kind):
        guard()
        name = 'phonoteka-'+scope+'-'+kind
        memory = str(LIMITS[kind+'_memory'])
        command = ['create', '--pull=never', '--name', name, '--label', 'io.phonoteka.acceptance='+scope,
                   '--no-healthcheck', '--network', 'none', '--read-only', '--user', f'{os.getuid()}:{os.getgid()}',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '256',
                   '--cpus', str(LIMITS[kind+'_cpus']), '--memory', memory, '--memory-swap', memory,
                   '--tmpfs', '/tmp:rw,size=128m,mode=1777', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                   '--env', 'ACCEPTANCE_REVISION='+args.revision,
                   '--mount', f'type=bind,source={root},target=/checks,readonly',
                   '--mount', f'type=bind,source={source},target=/expected-source,readonly',
                   '--mount', f'type=bind,source={root / "performance_acceptance.py"},target=/performance_acceptance.py,readonly',
                   '--mount', f'type=bind,source={out/kind},target=/reports']
        if kind == 'inference':
            command += ['--tmpfs', '/scratch:rw,size=160m,mode=1777', '-e', 'WORK_ROOT=/scratch/work',
                        '-e', 'INFERENCE_STATE=/scratch/inference', '-e', 'INFERENCE_SECRET_FILE=/scratch/key',
                        '-e', 'METRICS_TOKEN_FILE=/scratch/metrics-key']
            lane = ['/checks/inference_resource_acceptance.py']
        else:
            lane = ['/checks/performance_acceptance.py', '--real-audio-workers', '--duration-seconds', '480',
                    '--samples', '960', '--records', '1000', '--ready-file', '/reports/ready.json',
                    '--output', '/reports/result.json']
        command += ['--entrypoint', 'python', images[kind], '/checks/acceptance_image_probe.py', *lane]
        # Record intent before create. A lost response is recovered by this exact
        # unique name and then the full immutable ownership predicate, never by list/prune.
        owned[kind] = {'name': name, 'cid': None}
        try:
            cid = call(command, timeout=30).stdout.strip()
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            found = call(['inspect', name], check=False)
            if found.returncode == 0:
                cid = json.loads(found.stdout)[0]['Id']
                verify_owned(json.loads(found.stdout)[0], cid, name, images[kind], scope, kind)
                owned[kind]['cid'] = cid
            raise
        owned[kind]['cid'] = cid
        inspect(kind)
        call(['start', cid], timeout=30)
        # Running is proven before the separate 180-second measurement readiness wait.
        item = inspect(kind)
        if not item['State']['Running'] or item['State']['OOMKilled']:
            raise RuntimeError(kind+'_not_running')

    started = time.monotonic()
    try:
        if available() < sum(LIMITS[k] for k in ('inference_memory', 'ordinary_memory', 'reserved_host_memory')):
            raise RuntimeError('insufficient_memory_before_start')
        for image in images.values():
            info = json.loads(call(['image', 'inspect', image]).stdout)[0]
            if info['Id'] != image or info['Config']['Labels']['org.opencontainers.image.revision'] != args.revision:
                raise RuntimeError('image_revision_mismatch')
        launch('ordinary')
        readiness_deadline = time.monotonic()+180
        while True:
            guard()
            if time.monotonic() > readiness_deadline:
                raise TimeoutError('ordinary_readiness_timeout')
            if not inspect('ordinary')['State']['Running']:
                raise RuntimeError('ordinary_exited_before_readiness')
            path = out/'ordinary/ready.json'
            if path.exists():
                readiness = bounded_json(path)
                if not measured_readiness(readiness, started, time.monotonic()):
                    raise RuntimeError('ordinary_readiness_invalid')
                result['ordinary_measurement_readiness'] = readiness
                break
            time.sleep(.5)
        launch('inference')
        while True:
            guard()
            states = [inspect(kind)['State'] for kind in owned]
            if any(s['OOMKilled'] or (not s['Running'] and s['ExitCode'] != 0) for s in states):
                raise RuntimeError('lane_failed_or_oom')
            if all(not s['Running'] for s in states):
                break
            time.sleep(1)
    except Exception as exc:
        failure = type(exc).__name__+':'+str(exc)[:200]
    finally:
        for kind, entry in owned.items():
            try:
                if entry['cid'] is None:
                    raise RuntimeError('create_outcome_unknown')
                item = inspect(kind)
                log = out/(kind+'.console.log')
                with log.open('w') as stream:
                    subprocess.run(['docker', 'logs', '--tail', '200', entry['cid']], stdout=stream,
                                   stderr=subprocess.STDOUT, timeout=15, check=True)
                if item['State']['Running']:
                    call(['stop', '--time', '20', entry['cid']], timeout=35)
                item = inspect(kind)
                cleanup.append({'kind': kind, 'cid': entry['cid'], 'state': item['State'], 'removed': False})
                call(['rm', entry['cid']])
                absent = call(['inspect', entry['cid']], check=False)
                cleanup[-1]['removed'] = absent.returncode != 0 and 'No such' in absent.stderr
                if not cleanup[-1]['removed']:
                    raise RuntimeError('cleanup_not_verified')
            except Exception as exc:
                failure = failure or 'cleanup:'+type(exc).__name__
                result.setdefault('cleanup_errors', []).append({'kind': kind, 'error_type': type(exc).__name__})
        result.update(failure=failure, cleanup=cleanup, host_available_memory=pressure,
                      elapsed_seconds=time.monotonic()-started)
        try:
            ordinary = bounded_json(out/'ordinary/result.json')
            inference = bounded_json(out/'inference/inference-result.json')
            codes = [c['state']['ExitCode'] for c in cleanup if c['removed'] and not c['state']['OOMKilled']]
            result['checks'] = combined_checks(ordinary, inference, failure, codes)
            result['inference_checks_complete'] = (set(inference.get('checks', {})) == ASR_CHECKS
                                                    and all(v is True for v in inference['checks'].values()))
            result['source_verified'] = all(bounded_json(out/k/'source.json')['passed'] is True for k in images)
            result['passed'] = (set(result['checks']) == CHECKS and all(result['checks'].values())
                                and result['inference_checks_complete'] and result['source_verified']
                                and len(cleanup) == 2 and all(c['removed'] for c in cleanup))
        except Exception as exc:
            result['assessment_error_type'] = type(exc).__name__
        (out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({'passed': result['passed'], 'checks': result.get('checks'), 'failure': failure, 'report': str(out/'result.json')}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
