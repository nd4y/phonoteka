import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from phonoteka import db, exports, media
from phonoteka.app import create_app
from test_backend import settings, authenticated
from test_exports import published


def add_evidence(settings, track_id, text):
    row = db.fetchone(settings.db_path, 'SELECT metadata FROM tracks WHERE id=?', (track_id,))
    metadata = json.loads(row['metadata'])
    metadata['youtube'] = {'evidence_note': text}
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), track_id))


def test_generated_manifest_above_ten_mib_is_accessible(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        track_id, sha, target = published(settings)
        # UTF-8 bytes, not character count: reproduces a real multi-track
        # manifest crossing the old 10 MiB reader limit, using synthetic data.
        evidence = 'я' * (5 * 1024**2 + 128)
        add_evidence(settings, track_id, evidence)
        identity = exports.generate(settings, layout='artist_album')['export_id']
        path = settings.state_dir / 'exports' / identity / 'manifest.json'
        assert 10 * 1024**2 < path.stat().st_size < 32 * 1024**2
        response = client.get(f'/api/exports/{identity}/manifest.json')
        assert response.status_code == 200
        document = response.json()
        assert document['files'][0]['youtube']['evidence_note'] == evidence
        assert client.get('/api/exports/latest').json()['export']['id'] == identity
        relative = document['files'][0]['path']
        audio = client.get(f'/api/exports/{identity}/files/' + quote(relative, safe='/'))
        assert audio.status_code == 200 and audio.content == target.read_bytes()
        receipt = client.post('/api/deliveries', headers=headers, json={
            'export_id': identity, 'device': 'large-manifest-fixture',
            'checksums': {relative: sha}, 'poweramp_checked': False,
        })
        assert receipt.status_code == 201 and receipt.json()['state'] == 'client_hashes_match'


def test_oversized_generation_keeps_previous_snapshot_before_copy(settings, monkeypatch):
    db.initialize(settings.db_path)
    track_id, _, target = published(settings)
    identity = exports.generate(settings)['export_id']
    original = target.read_bytes()
    base = settings.state_dir / 'exports'
    before = set(base.iterdir())
    add_evidence(settings, track_id, 'я' * 1024)
    monkeypatch.setattr(exports, 'MAX_MANIFEST_BYTES', 2048)

    def unexpected_copy(*args, **kwargs):
        pytest.fail('Oversized manifest started copying audio')

    monkeypatch.setattr(media, '_copy', unexpected_copy)
    with pytest.raises(media.MediaError) as error:
        exports.generate(settings)
    assert error.value.code == 'export_manifest_too_large'
    assert db.setting(settings.db_path, 'latest_export') == identity
    assert set(base.iterdir()) == before and target.read_bytes() == original


def test_oversized_existing_manifest_is_not_reported_missing(settings, monkeypatch):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        published(settings)
        identity = exports.generate(settings)['export_id']
        path = settings.state_dir / 'exports' / identity / 'manifest.json'
        monkeypatch.setattr(exports, 'MAX_MANIFEST_BYTES', path.stat().st_size - 1)
        response = client.get(f'/api/exports/{identity}/manifest.json')
        assert response.status_code == 409
        assert response.json()['code'] == 'export_manifest_too_large'
        assert path.is_file()
