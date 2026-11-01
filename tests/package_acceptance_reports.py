"""Archive only generated acceptance reports for a private draft CI release."""
import argparse
import hashlib
from pathlib import Path
import tarfile

ALLOWED = {'result.json', 'ordinary.console.log', 'inference.console.log',
           'ordinary/ready.json', 'ordinary/result.json', 'ordinary/source.json',
           'inference/inference-result.json', 'inference/inference-service.log', 'inference/source.json'}


def package(root: Path, target: Path):
    files = sorted(p for p in root.rglob('*') if p.is_file() or p.is_symlink())
    names = {p.relative_to(root).as_posix() for p in files}
    if (root.is_symlink() or not root.is_dir() or not files or 'result.json' not in names
            or not names <= ALLOWED or any(p.is_symlink() or not p.is_file() for p in files)
            or any(p.stat().st_size > 8*1024**2 for p in files)
            or sum(p.stat().st_size for p in files) > 32*1024**2 or target.exists()):
        raise ValueError('unsafe_or_unbounded_acceptance_archive')
    with tarfile.open(target, 'x:gz') as archive:
        for p in files:
            archive.add(p, arcname=p.relative_to(root).as_posix(), recursive=False)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name+'.sha256').write_text(digest+'  '+target.name+'\n')
    return digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    print(package(args.root, args.target))
