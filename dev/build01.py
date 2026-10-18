"""Transfer source only, execute all builds/tests/demo on the shared build server."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile

REPO = Path(__file__).resolve().parent.parent
REMOTE_ROOT = '/home/av/build/phonoteka'


def execute(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'check-ui', 'start-demo', 'status', 'stop-demo'])
    parser.add_argument('--host', default='av@192.168.0.21')
    parser.add_argument('--snapshot', help='Previously tested source SHA256 for start-demo')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_.@-]+', args.host):
        raise SystemExit('Invalid SSH destination')
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.host]
    execute(ssh + [shlex.join(['mkdir', '-p', REMOTE_ROOT])])
    execute(['scp', '-q', str(REPO/'dev/build01_host.py'), f'{args.host}:{REMOTE_ROOT}/control.py'])
    if args.action not in {'run', 'check-ui'}:
        command = ['python3', '-u', REMOTE_ROOT+'/control.py', args.action]
        if args.snapshot:
            if args.action != 'start-demo' or not re.fullmatch('[a-f0-9]{64}', args.snapshot):
                raise SystemExit('--snapshot requires start-demo and a SHA256 value')
            command.append(args.snapshot)
        execute(ssh + [shlex.join(command)])
        return
    git = ['git', '-c', f'safe.directory={REPO.as_posix()}', '-C', str(REPO)]
    files = execute(git + ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True).stdout.decode('utf-8').split('\0')
    commit = execute(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    manifest = {'commit': commit, 'files': {}}
    with tempfile.TemporaryDirectory(prefix='phonoteka-source-') as temporary:
        archive = Path(temporary)/'source.tar'
        with tarfile.open(archive, 'w') as tar:
            for name in sorted(set(filter(None, files))):
                path = REPO/name
                if not path.exists():
                    continue
                if path.is_symlink() or not path.is_file():
                    raise SystemExit('Refusing non-regular source file: '+name)
                if (any(part in {'.git', '.venv', 'node_modules', 'secrets', 'state'} for part in Path(name).parts)
                        or path.name.startswith('.env') or re.search(r'\.(db|sqlite|key|pem)(-|\.|$)', name)
                        or name.startswith('deploy/st1.')):
                    continue
                data = path.read_bytes()
                manifest['files'][name] = hashlib.sha256(data).hexdigest()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
            data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
            info = tarfile.TarInfo('dev/snapshot.json')
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        print(json.dumps({'snapshot': digest, 'files': len(manifest['files']), 'bytes': archive.stat().st_size}), flush=True)
        execute(['scp', '-q', str(archive), f'{args.host}:{REMOTE_ROOT}/upload-{digest}.tar'])
    execute(ssh + [shlex.join(['python3', '-u', REMOTE_ROOT+'/control.py', args.action, digest])])


if __name__ == '__main__':
    main()
