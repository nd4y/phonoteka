"""Check installed backend bytes before executing an isolated acceptance lane."""
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import sys


def verify(expected: Path, installed: Path) -> dict:
    def files(root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*.py') if '__pycache__' not in p.parts}
    actual, wanted = files(installed), files(expected)
    if not wanted or actual != wanted:
        raise RuntimeError('installed_backend_source_mismatch')
    return {'passed': True, 'modules': len(wanted), 'sha256': wanted}


if __name__ == '__main__':
    spec = importlib.util.find_spec('phonoteka')
    proof = verify(Path('/expected-source'), Path(spec.origin).parent)
    Path('/reports/source.json').write_text(json.dumps(proof, indent=2))
    sys.argv = sys.argv[1:]
    # Preserve the legacy absolute import used by the resource harness.
    sys.path.insert(0, '/checks')
    runpy.run_path(sys.argv[0], run_name='__main__')
