"""Project-scoped controller executed on build-01. Never prunes shared Docker state."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path.home() / 'build/phonoteka'
ROOT.mkdir(parents=True, exist_ok=True)
ROOT.chmod(0o700)
DOCKER = ['docker', '-H', 'unix:///var/run/docker.sock']
BUILDER = 'phonoteka-build01'
DEMO = 'phonoteka-demo'
LABEL = 'io.phonoteka.dev'
current_path = ROOT / 'current.json'
CONTROLLER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def capture(args, timeout=30, required=True):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if required and r.returncode:
        raise RuntimeError(f'{args[:4]}: {r.stderr[-1800:]}')
    return r


def inspect(name):
    r = capture(DOCKER + ['inspect', name], required=False)
    return json.loads(r.stdout)[0] if r.returncode == 0 else None


def inventory():
    ids = capture(DOCKER + ['ps', '-q', '--no-trunc']).stdout.split()
    if not ids:
        return {}
    rows = json.loads(capture(DOCKER + ['inspect'] + ids).stdout)
    return {row['Id']: {'name': row['Name'], 'started': row['State']['StartedAt'],
                       'restarts': row['RestartCount'], 'builder': row['Name'].startswith('/buildx_buildkit_')} for row in rows}


def status():
    state = json.loads(current_path.read_text()) if current_path.exists() else {}
    container = inspect(DEMO)
    active = False
    with (ROOT / 'operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        except BlockingIOError:
            active = True
    logs = sorted((ROOT / 'reports').rglob('*.log'), key=lambda p: p.stat().st_mtime)
    latest = logs[-1] if logs else None
    emit(operation_active=active, latest_log=str(latest) if latest else None,
         current=state, demo=None if container is None else {
        'id': container['Id'], 'state': container['State']['Status'],
        'health': container['State'].get('Health', {}).get('Status'),
        'image': container['Image'], 'ports': container['NetworkSettings']['Ports'],
    })


def logged(args, name, report_dir, timeout=3600):
    path = report_dir / f'{name}.log'
    emit(stage=name, state='running', log=str(path))
    start = time.monotonic()
    try:
        with path.open('w') as stream:
            r = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        if name == 'backend-test':
            row = inspect('phonoteka-build01-tests')
            if row and row['Config'].get('Labels', {}).get(LABEL) == 'build01':
                capture(DOCKER + ['stop', '--time', '10', 'phonoteka-build01-tests'], timeout=25, required=False)
        emit(stage=name, state='timeout', log=str(path))
        raise
    emit(stage=name, state='passed' if r.returncode == 0 else 'failed', seconds=round(time.monotonic()-start, 1))
    if r.returncode:
        emit(tail='\n'.join(path.read_text(errors='replace').splitlines()[-35:]))
        raise RuntimeError(f'{name} failed: {r.returncode}')


def run(snapshot, ui_only=False):
    if len(snapshot) != 64 or any(c not in '0123456789abcdef' for c in snapshot):
        raise ValueError('Expected SHA256 snapshot')
    archive = ROOT / f'upload-{snapshot}.tar'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != snapshot:
        raise ValueError('Archive SHA256 mismatch')
    source = ROOT / 'sources' / snapshot
    if not source.exists():
        source.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or not member.isfile():
                    raise ValueError('Unsafe archive entry')
                dest = source / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(tar.extractfile(member).read())
    manifest = json.loads((source / 'dev/snapshot.json').read_text())
    for relative, digest in manifest['files'].items():
        if hashlib.sha256((source / relative).read_bytes()).hexdigest() != digest:
            raise ValueError('Snapshot file mismatch: ' + relative)
    run_id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-{os.getpid()}'
    reports = ROOT / 'reports' / snapshot / run_id
    reports.mkdir(parents=True, exist_ok=True)
    baseline = inventory()
    (reports / 'containers-before.json').write_text(json.dumps(baseline, indent=2))
    emit(stage='snapshot-verified', snapshot=snapshot, commit=manifest['commit'], files=len(manifest['files']))
    existing = capture(DOCKER + ['buildx', 'inspect', BUILDER], required=False)
    if existing.returncode:
        capture(DOCKER + ['buildx', 'create', '--name', BUILDER, '--driver', 'docker-container',
                          '--driver-opt', 'memory=2g,cpu-period=100000,cpu-quota=200000'])
    image = f'phonoteka-build01:demo-{snapshot[:12]}'
    common = DOCKER + ['buildx', 'build', '--builder', BUILDER, '--progress=plain',
                      '--label', f'io.phonoteka.source_sha256={snapshot}',
                      '--label', f'org.opencontainers.image.revision={manifest["commit"]}']
    combined = source / 'dev/Dockerfile.build01'
    dockerfile = (source/'Dockerfile').read_text()
    needle = 'RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg'
    if dockerfile.count(needle) != 1:
        raise RuntimeError('Review the dev-only package-install optimization after Dockerfile changes')
    # Only the disposable dev image's package installation skips fsync. No global
    # LD_PRELOAD, runtime setting or production Dockerfile is changed.
    dockerfile = dockerfile.replace(needle,
        'RUN apt-get update && apt-get install -y --no-install-recommends eatmydata '
        '&& eatmydata apt-get install -y --no-install-recommends ffmpeg')
    combined.write_text(dockerfile + '\n' + (source/'dev/Dockerfile').read_text())
    # Include tests for this target only; production .dockerignore stays unchanged.
    Path(str(combined)+'.dockerignore').write_text('\n'.join(
        line for line in (source/'.dockerignore').read_text().splitlines() if line.strip() != 'tests'
    ) + '\n')
    try:
        logged(common + ['--target', 'frontend-test', '-f', str(source/'Dockerfile'), str(source)], 'frontend-build-test', reports)
        if ui_only:
            emit(stage='ui-check-passed', snapshot=snapshot, reports=str(reports), demo_changed=False)
            return
        logged(common + ['--load', '--target', 'demo', '-t', image, '-f', str(combined), str(source)], 'runtime-demo-build', reports)
    finally:
        capture(DOCKER + ['buildx', 'stop', BUILDER], timeout=60, required=False)
    uid = f'{os.getuid()}:{os.getgid()}'
    logged(DOCKER + ['run', '--rm', '--no-healthcheck', '--name', 'phonoteka-build01-tests', '--label', f'{LABEL}=build01',
           '--cpus', '2', '--memory', '1536m', '--user', uid, '--env', 'HOME=/tmp',
           '--tmpfs', '/tmp:size=1g,mode=1777',
           '--volume', f'{reports}:/reports', image, 'python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
           '/app/tests', '--junitxml=/reports/backend.xml'], 'backend-test', reports, 600)
    launch_demo(snapshot, source, manifest, image, reports, baseline, reports)


def start_tested(snapshot=None):
    candidates = sorted((ROOT/'reports').rglob('backend.xml'), key=lambda p: p.stat().st_mtime, reverse=True)
    for junit in candidates:
        candidate = junit.relative_to(ROOT/'reports').parts[0]
        if snapshot and candidate != snapshot:
            continue
        if len(candidate) != 64 or any(c not in '0123456789abcdef' for c in candidate):
            continue
        suites = list(ET.parse(junit).getroot().iter('testsuite'))
        if not suites or sum(int(s.get('tests', 0)) for s in suites) == 0:
            continue
        if any(int(s.get('failures', 0)) or int(s.get('errors', 0)) for s in suites):
            continue
        source = ROOT/'sources'/candidate
        manifest = json.loads((source/'dev/snapshot.json').read_text())
        image = f'phonoteka-build01:demo-{candidate[:12]}'
        built = inspect(image)
        if not built or built['Config'].get('Labels', {}).get('io.phonoteka.source_sha256') != candidate:
            continue
        running = inspect(DEMO)
        if (running and running['State'].get('Running') and running['Image'] == built['Id']
                and running['Config'].get('Labels', {}).get(LABEL) == 'build01'):
            emit(stage='already-running', snapshot=candidate, container_id=running['Id'])
            status()
            return
        run_id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-start-{os.getpid()}'
        reports = ROOT/'reports'/candidate/run_id
        reports.mkdir(parents=True)
        baseline = inventory()
        (reports/'containers-before.json').write_text(json.dumps(baseline, indent=2))
        emit(stage='reuse-tested-image', snapshot=candidate, image_id=built['Id'], verified_tests=str(junit))
        launch_demo(candidate, source, manifest, image, reports, baseline, junit.parent)
        return
    raise RuntimeError('No matching image with a passing backend report; run a full build first')


def launch_demo(snapshot, source, manifest, image, reports, baseline, verified_reports):
    uid = f'{os.getuid()}:{os.getgid()}'
    data = ROOT / 'demo'
    data.mkdir(mode=0o700, exist_ok=True)
    old = inspect(DEMO)
    previous = None
    stop_issued = False
    replacement_attempt = f'{snapshot}:{time.time_ns()}'

    def stopped(row):
        state = row['State']
        return (not state['Running'] and not state.get('Restarting') and not state.get('Dead')
                and state['Status'] in {'exited', 'created'} and state.get('Pid') == 0)

    def owned_old():
        row = inspect(old['Id'])
        if (not row or row['Id'] != old['Id'] or row['Config'].get('Labels', {}).get(LABEL) != 'build01'
                or row['Name'] not in {'/' + DEMO, '/' + previous}):
            raise RuntimeError('Previous demo identity could not be verified; nothing else stopped')
        return row

    if old and old['Config'].get('Labels', {}).get(LABEL) != 'build01':
        raise RuntimeError('Refusing to replace a container owned by another process')
    try:
        if old:
            previous = f'{DEMO}-previous-{time.time_ns()}'
            capture(DOCKER + ['rename', old['Id'], previous])
            stop_issued = True
            try:
                # Keep Docker's 20s grace; allow time for daemon response/cleanup.
                capture(DOCKER + ['stop', '--time', '20', old['Id']], timeout=90)
            except subprocess.TimeoutExpired:
                if not stopped(owned_old()):
                    raise
                emit(stage='demo-previous-stop', state='confirmed-stopped-after-cli-timeout',
                     container_id=old['Id'])
            if not stopped(owned_old()):
                raise RuntimeError('Previous demo is not confirmed stopped')
        capture(DOCKER + ['run', '-d', '--name', DEMO, '--label', f'{LABEL}=build01',
               '--label', f'io.phonoteka.demo_replacement={replacement_attempt}',
               '--label', f'io.phonoteka.snapshot={snapshot}', '--restart', 'unless-stopped',
               '--cpus', '1', '--memory', '512m', '--user', uid, '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--tmpfs', '/tmp:size=128m,mode=1777', '--pids-limit', '128',
               '--log-opt', 'max-size=10m', '--log-opt', 'max-file=2',
               '--publish', '127.0.0.1:8177:8000', '--volume', f'{data}:/demo', image])
        for attempt in range(300):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8177/health', timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError('Demo health did not become ready')
    except Exception:
        # Rollback failure must preserve the original controller failure. A
        # pending daemon stop is not permission to force-kill/restart another ID.
        try:
            failed = inspect(DEMO)
            if failed and (not old or failed['Id'] != old['Id']):
                labels = failed['Config'].get('Labels', {})
                if labels.get(LABEL) != 'build01' or labels.get('io.phonoteka.demo_replacement') != replacement_attempt:
                    raise RuntimeError('DEMO belongs to another attempt; rollback left it untouched')
                try:
                    logs = capture(DOCKER + ['logs', '--tail', '100', failed['Id']], timeout=10, required=False)
                    (reports/'demo-startup-failed.log').write_text(logs.stdout+logs.stderr)
                except Exception as log_error:
                    emit(stage='demo-rollback-log', state='unavailable', error_type=type(log_error).__name__)
                capture(DOCKER + ['rm', '-f', failed['Id']])
            if old:
                row = owned_old()
                if row['Name'] != '/' + DEMO:
                    capture(DOCKER + ['rename', old['Id'], DEMO])
                if old['State']['Running'] and stopped(row):
                    capture(DOCKER + ['start', old['Id']], timeout=90)
                elif old['State']['Running'] and stop_issued:
                    emit(stage='demo-rollback', state='uncertain-shutdown-in-progress',
                         container_id=old['Id'])
        except Exception as rollback_error:
            emit(stage='demo-rollback', state='unverified', error_type=type(rollback_error).__name__)
        raise
    if old:
        if not stopped(owned_old()):
            raise RuntimeError('Previous demo changed state; refusing cleanup')
        capture(DOCKER + ['rm', old['Id']])

    after = inventory()
    unrelated_changes = {key: value for key, value in baseline.items()
                         if value['name'] != '/' + DEMO and after.get(key) != value}
    persistent_changes = {key: value for key, value in unrelated_changes.items() if not value.get('builder')}
    (reports / 'containers-after.json').write_text(json.dumps(after, indent=2))
    result = {'snapshot': snapshot, 'commit': manifest['commit'], 'image': image,
              'controller_sha256': CONTROLLER_SHA256,
              'verified_tests_report': str(verified_reports),
              'reports': str(reports), 'source': str(source), 'url': 'http://127.0.0.1:8177',
              'concurrent_container_changes': unrelated_changes,
              'persistent_services_changed': persistent_changes}
    temporary = current_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(current_path)
    emit(stage='ready', **result)
    if persistent_changes:
        # A shared server has independent owners. Preserve observations for
        # review; another task finishing must not invalidate our successful build.
        emit(warning='Other container lifecycle changed; review the before/after report',
             observed_changes=persistent_changes)


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'status':
        status()
    else:
        with (ROOT / 'operation.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit('Another Phonoteka operation is active; nothing interrupted')
            if action in {'run','check-ui'}:
                run(sys.argv[2], ui_only=action=='check-ui')
            elif action == 'start-demo':
                start_tested(sys.argv[2] if len(sys.argv)>2 else None)
            elif action == 'stop-demo':
                row = inspect(DEMO)
                if row and row['Config'].get('Labels', {}).get(LABEL) == 'build01':
                    capture(DOCKER + ['stop', '--time', '20', DEMO], timeout=35)
            else:
                raise SystemExit('Expected run, start-demo, status or stop-demo')
