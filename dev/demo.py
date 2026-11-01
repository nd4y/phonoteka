"""Synthetic, isolated preview catalog. Never mount real music into this demo."""
from pathlib import Path
import json
import os
import secrets
import wave

import uvicorn
from fastapi.responses import JSONResponse
from mutagen.id3 import SYLT, TALB, TIT2, TPE1
from mutagen.wave import WAVE
from phonoteka import cue, db, media, migrations
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
    owner_password_file=password, app_url=os.environ.get('DEMO_URL', 'http://127.0.0.1:8177'),
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


def insert(identity, path):
    # Seed from the same native readers used by a real collection scan. Tags,
    # synchronized lyrics and CUE availability must survive that later scan.
    metadata = (cue.inspect_cue(path, settings.work_root) if path.suffix == '.cue'
                else media.inspect_audio(path, settings.roots))
    for field in ('lrc', 'lyrics'):
        metadata[field] = metadata.get(field) or ''
    metadata['hash_verified_at'] = db.utcnow()
    with db.transaction(settings.db_path) as conn:
        conn.execute(
            'INSERT OR IGNORE INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            (identity, path.relative_to(settings.work_root).as_posix(), metadata['sha256'],
             db.encode(metadata), db.utcnow(), db.utcnow()),
        )


for i in range(1, 44):
    path = settings.work_root / f'{i:03}.wav'
    if not path.exists():
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b'\0\0' * 8000 * 30)
        audio = WAVE(path)
        audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=[f'Тестовый трек {i:02}']))
        audio.tags.add(TPE1(encoding=3, text=['Проверка на build-01']))
        audio.tags.add(TALB(encoding=3, text=['Демо плеера']))
        audio.tags.add(SYLT(encoding=3, lang='rus', format=2, type=1,
                            desc='Синтетические строки',
                            text=[('Первая строка', 0), ('Вторая строка', 5000)]))
        audio.save(v2_version=4)
    # Existing demo files are deliberately left byte-for-byte unchanged.
    insert(f'fixture-{i:03}', path)
path = settings.work_root / '000.cue'
if not path.exists():
    path.write_text('''REM synthetic fixture
TITLE "Тестовый CUE"
PERFORMER "Тест"
FILE "001.wav" WAVE
  TRACK 01 AUDIO
    TITLE "Композиция 1"
    INDEX 01 00:00:00
FILE "missing-synthetic-track.wav" WAVE
  TRACK 02 AUDIO
    TITLE "Композиция 2 - нет аудио"
    INDEX 01 00:00:00
FILE "001.wav" WAVE
  TRACK 03 AUDIO
    TITLE "Композиция 3"
    INDEX 01 00:05:00
''', encoding='utf-8')
insert('fixture-cue', path)
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
