"""Synthetic, isolated preview catalog. Never mount real music into this demo."""
from pathlib import Path
import hashlib
import json
import os
import secrets
import wave

import uvicorn
from fastapi.responses import JSONResponse
from phonoteka import cue, db, migrations
from phonoteka.app import create_app
from phonoteka.config import Settings
from phonoteka.telemetry import configure_logging

configure_logging('demo')

base = Path(os.environ.get('DEMO_DATA', '/demo'))
for name in ('state', 'work', 'original', 'publication', 'guard'):
    (base / name).mkdir(parents=True, exist_ok=True)
password = base / 'owner_password'
if not password.exists():
    password.write_text(secrets.token_urlsafe(24), encoding='utf-8')
    password.chmod(0o600)
settings = Settings(
    state_dir=base / 'state', work_root=base / 'work', source_root=base / 'original',
    publication_root=base / 'publication', storage_guard_dir=base / 'guard',
    owner_password_file=password, app_url='http://127.0.0.1:8177',
    allow_insecure_localhost=True, static_dir=Path('/app/frontend/dist'),
    read_only=False, storage_verified=True, instance_id='build01-synthetic-demo',
)
(base / 'guard/deployment.json').write_text(json.dumps({
    'storage_domain': 'synthetic-demo', 'instance_id': settings.instance_id,
    'work_root': str(settings.work_root), 'source_root': str(settings.source_root),
    'publication_root': str(settings.publication_root),
}), encoding='utf-8')
# This entry point serves only generated fixtures. The deployment script stops
# the previous demo container before starting it; production startup never does
# an automatic schema transition.
if settings.db_path.exists() and migrations.status(settings)['schema']==1:
    migrations.migrate(settings,confirm_stopped=True)
db.initialize(settings.db_path,settings=settings)


def insert(identity, path, metadata):
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata['sha256'] = sha
    with db.transaction(settings.db_path) as conn:
        conn.execute(
            'INSERT OR IGNORE INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            (identity, path.name, sha, db.encode(metadata), db.utcnow(), db.utcnow()),
        )


for i in range(1, 44):
    path = settings.work_root / f'{i:03}.wav'
    if not path.exists():
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\0\0' * 8000 * 30)
    insert(f'fixture-{i:03}', path, {
        'format': 'wav', 'duration': 30, 'size': path.stat().st_size, 'write_supported': True,
        'tags': {'title': [f'Тестовый трек {i:02}'], 'artist': ['Проверка на build-01'], 'album': ['Демо плеера']},
        'lrc': '[00:00.000]Первая строка\n[00:05.000]Вторая строка',
    })
path = settings.work_root / '000.cue'
if not path.exists():
    path.write_text('REM synthetic fixture', encoding='utf-8')
insert('fixture-cue', path, {
    'format': 'cue', 'write_supported': False, 'tags': {'title': ['Тестовый CUE']},
    'cue': {'title': 'Тестовый CUE', 'performer': 'Тест', 'encoding': 'utf-8', 'errors': [], 'warnings': [],
            'files': [{'relative_path': '001.wav', 'declared_name': '001.wav', 'signature': cue.signature(settings.work_root / '001.wav')}],
            'tracks': [{'number': i, 'title': f'Композиция {i}', 'performer': 'Тест', 'file_index': 0,
                        'available': i != 2, 'start_seconds': (i-1)*5, 'end_seconds': i*5, 'duration': 5} for i in (1, 2, 3)]},
})
app = create_app(settings)


@app.middleware('http')
async def fail_one(request, call_next):
    marker = base / 'fail-detail-once'
    if marker.exists() and request.url.path == '/api/tracks/' + marker.read_text().strip():
        marker.unlink()
        return JSONResponse({'message': 'Синтетическая ошибка загрузки'}, status_code=503)
    return await call_next(request)


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000, access_log=False, log_config=None)
