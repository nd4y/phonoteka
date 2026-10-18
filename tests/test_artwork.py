import base64
import io
import json
import subprocess
import uuid

from PIL import Image
import pytest
from concurrent.futures import ThreadPoolExecutor
import time

from phonoteka import artwork, db, media, provider_policy
from phonoteka.artwork_worker import transform
from test_backend import authenticated, client, settings
from test_metadata_slots import generated, selected
from test_media import TOOLS


def image_bytes(size=(2000, 1000), *, mode='RGB', color='red', format='PNG'):
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format=format)
    return output.getvalue()


def test_real_image_resize_preserves_aspect_and_respects_byte_budget():
    source = image_bytes()
    result = transform(source, {'max_edge': 1200, 'max_bytes': 1536 * 1024, 'format': 'jpeg', 'quality': 88})
    assert (result['source']['width'], result['source']['height']) == (2000, 1000)
    assert (result['result']['width'], result['result']['height']) == (1200, 600)
    assert result['result']['bytes'] <= 1536 * 1024 and not result['profile']['crop'] and not result['profile']['upscale']
    with Image.open(io.BytesIO(base64.b64decode(result['image_b64']))) as actual:
        actual.load(); assert actual.format == 'JPEG' and actual.size == (1200, 600)
    noise = Image.effect_noise((800, 400), 100).convert('RGB'); output = io.BytesIO(); noise.save(output, 'PNG'); noise.close()
    limited = transform(output.getvalue(), {'max_edge': 800, 'max_bytes': 32768, 'format': 'png', 'quality': 88})
    assert limited['result']['bytes'] <= 32768 and limited['result']['width'] < 800
    assert abs(limited['result']['width'] / limited['result']['height'] - 2) < .03
    jpeg_budget = transform(output.getvalue(), {'max_edge': 800, 'max_bytes': 32768, 'format': 'jpeg', 'quality': 88})
    assert jpeg_budget['result']['bytes'] <= 32768 and jpeg_budget['profile']['effective_quality'] <= 88


def test_alpha_to_jpeg_uses_white_background_and_never_upscales():
    result = transform(image_bytes((64, 32), mode='RGBA', color=(0, 0, 0, 0)), {'max_edge': 1200, 'max_bytes': 32768, 'format': 'jpeg', 'quality': 95})
    with Image.open(io.BytesIO(base64.b64decode(result['image_b64']))) as actual:
        assert actual.size == (64, 32) and all(channel >= 250 for channel in actual.getpixel((0, 0)))


def test_image_validation_limits_actual_decode_and_subprocess_deadline(monkeypatch):
    with pytest.raises(ValueError, match='25 megapixels'): transform(image_bytes((5001, 5000)))
    with pytest.raises(media.MediaError) as oversized: artwork.process(b'x' * (20 * 1024 * 1024 + 1))
    assert oversized.value.code == 'invalid_cover'
    with pytest.raises(media.MediaError): artwork.process(image_bytes((64, 32), format='GIF'))
    with pytest.raises(media.MediaError): artwork.process(b'\x89PNG\r\n\x1a\n')
    assert artwork.process(image_bytes((64, 32)))['source']['width'] == 64
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 10 and kwargs['stderr'] is subprocess.DEVNULL
        raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr(artwork.subprocess, 'run', timeout)
    with pytest.raises(media.MediaError) as bounded: artwork.process(image_bytes((67, 31), color='blue'))
    assert bounded.value.code == 'invalid_cover'


def test_concurrent_validation_waits_for_one_decode_and_caches_only_small_sha_descriptions(monkeypatch):
    raw = image_bytes((73, 37), color='green'); original = artwork.subprocess.run; calls = []
    def slow(*args, **kwargs):
        calls.append(True); time.sleep(.35); return original(*args, **kwargs)
    monkeypatch.setattr(artwork.subprocess, 'run', slow)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: artwork.process(raw), range(30)))
    assert len(calls) == 1 and all(result['source']['width'] == 73 for result in results)
    with artwork.CACHE_LOCK:
        assert len(artwork.VALIDATED) <= 512
        assert all(isinstance(key, str) and len(key) == 64 for key in artwork.VALIDATED)
        assert all(set(value) == {'sha256', 'bytes', 'width', 'height', 'mime'} for value in artwork.VALIDATED.values())


@TOOLS
def test_artwork_preview_is_owner_cas_bound_and_requires_smaller_confirmation(client, settings):
    path = generated(settings.work_root, 'flac'); meta = media.inspect_audio(path, settings.roots); track_id = str(uuid.uuid4())
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)', (track_id, path.name, meta['sha256'], db.encode(meta), db.utcnow(), db.utcnow()))
    choice = selected(media._load_audio(path)); url = '/api/tracks/' + track_id; raw = image_bytes((20, 10))
    source = 'data:image/png;base64,' + base64.b64encode(raw).decode()
    body = {'source': source, 'slot_selection': choice, 'max_edge': 64, 'max_bytes': 32768, 'format': 'jpeg', 'quality': 88}
    assert client.post(url + '/artwork-preview', json=body).status_code == 401
    headers = {**authenticated(client, settings), 'If-Match': meta['sha256']}
    assert client.post(url + '/artwork-preview', headers={'If-Match': meta['sha256']}, json=body).status_code == 403
    preview = client.post(url + '/artwork-preview', headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    data = preview.json(); assert data['smaller_than_existing'] and 'image_b64' not in data
    proposal = {'cover': data['cover'], 'slot_selection': choice, 'artwork_preview_id': data['preview_id']}
    assert client.post(url + '/proposal', headers=headers, json=proposal).status_code == 409
    accepted = client.post(url + '/proposal', headers=headers, json={**proposal, 'confirm_smaller_cover': True})
    assert accepted.status_code == 201, accepted.text
    stored = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (accepted.json()['id'],))['payload'])
    assert stored['artwork_transform']['source']['width'] == 20 and stored['artwork_transform']['result']['width'] == 20
    assert stored['artwork_transform']['smaller_confirmed_by_owner'] is True
    record = db.fetchone(settings.db_path, 'SELECT value FROM settings WHERE key=?', (artwork.PREFIX + data['preview_id'],))['value']
    assert source not in record and data['cover'] not in record
    changed = {**proposal, 'confirm_smaller_cover': True, 'slot_selection': {**choice, 'cover_slot_id': 'new'}}
    assert client.post(url + '/proposal', headers=headers, json=changed).status_code in {409, 412}
    assert media.sha256_file(path) == meta['sha256']


@TOOLS
def test_resizing_known_provider_content_keeps_restrictions_on_manual_copy(client, settings):
    from test_provider_policy import grant
    path = generated(settings.work_root, 'flac'); meta = media.inspect_audio(path, settings.roots); track_id = str(uuid.uuid4())
    raw = image_bytes((100, 50)); source = 'data:image/png;base64,' + base64.b64encode(raw).decode()
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)', (track_id, path.name, meta['sha256'], db.encode(meta), db.utcnow(), db.utcnow()))
        grant(conn, provider='coverartarchive', kind='cover', allow_export=False)
        candidate = {'kind': 'cover', 'provider': 'coverartarchive', 'cover': source}
        usage = provider_policy.candidate_usage(conn, candidate)
        provider_policy.register_values(conn, candidate, usage)
    choice = selected(media._load_audio(path)); headers = {**authenticated(client, settings), 'If-Match': meta['sha256']}
    response = client.post('/api/tracks/' + track_id + '/artwork-preview', headers=headers, json={'source': source, 'slot_selection': choice})
    assert response.status_code == 200, response.text
    row = db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,))
    with db.connect(settings.db_path) as conn:
        with pytest.raises(media.MediaError) as denied: provider_policy.require_payload(conn, row, {'cover': response.json()['cover']}, 'export')
    assert denied.value.code == 'provider_policy_denied'


@TOOLS
def test_old_permitted_cover_cannot_override_new_sources_stricter_rights(client, settings, monkeypatch):
    from test_provider_policy import grant
    path = generated(settings.work_root, 'flac'); meta = media.inspect_audio(path, settings.roots); track_id = str(uuid.uuid4())
    source = 'data:image/png;base64,' + base64.b64encode(image_bytes((100, 50))).decode()
    with db.transaction(settings.db_path) as conn:
        grant(conn, provider='coverartarchive', kind='cover')
        old_usage = provider_policy.candidate_usage(conn, {'provider': 'coverartarchive', 'kind': 'cover'})
        meta['usage_provenance'] = {'cover': {'usage': old_usage}}
        conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)', (track_id, path.name, meta['sha256'], db.encode(meta), db.utcnow(), db.utcnow()))
        grant(conn, provider='theaudiodb', kind='cover', allow_derived_analysis=False, allow_export=False)
        candidate = {'provider': 'theaudiodb', 'kind': 'cover', 'cover': source}
        provider_policy.register_values(conn, candidate, provider_policy.candidate_usage(conn, candidate))
    def forbidden_transform(*args, **kwargs): raise AssertionError('Disallowed source reached image processing')
    monkeypatch.setattr(artwork, 'process', forbidden_transform)
    headers = {**authenticated(client, settings), 'If-Match': meta['sha256']}
    response = client.post('/api/tracks/' + track_id + '/artwork-preview', headers=headers, json={'source': source, 'slot_selection': selected(media._load_audio(path))})
    assert response.status_code == 409 and response.json()['code'] == 'provider_policy_denied'
