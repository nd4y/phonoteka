"""Selected second native slots remain the exact reviewed/exported sources."""
import base64
from dataclasses import replace
import hashlib
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from phonoteka import db, exports, media, metadata_slots, publication_sidecars as sidecars
from phonoteka.app import create_app
from test_backend import settings, authenticated, accept_publication_review, job, drain
from test_media import LINUX, TOOLS, cover_bytes
from test_metadata_slots import generated, selected


@LINUX
@TOOLS
def test_second_lyric_and_resized_front_are_frozen_into_ready_sidecars_and_export(settings):
    cfg = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        path = generated(cfg.work_root, 'flac'); metadata = media.inspect_audio(path, cfg.roots); identity = str(uuid.uuid4())
        metadata['usage_provenance'] = {field: {'origin': 'preexisting', 'reason': 'retained_first_native'} for field in ('cover', 'lrc')}
        with db.transaction(cfg.db_path) as conn:
            conn.execute('INSERT INTO tracks(id,relative_path,sha256,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)', (identity, path.name, metadata['sha256'], db.encode(metadata), db.utcnow(), db.utcnow()))
        choice = selected(media._load_audio(path)); url = '/api/tracks/' + identity
        preview = client.post(url + '/artwork-preview', headers={**headers, 'If-Match': metadata['sha256']},
                              json={'source': 'data:image/png;base64,' + base64.b64encode(cover_bytes('yellow')).decode(), 'slot_selection': choice})
        assert preview.status_code == 200, preview.text
        transformed = preview.json()
        proposal = client.post(url + '/proposal', headers={**headers, 'If-Match': metadata['sha256']}, json={
            'tags': {'title': 'Generated selected slots', 'artist': 'Fixture owner', 'album': 'Fixture album'},
            'lrc': '[00:00.500]Owner reviewed second', 'cover': transformed['cover'], 'slot_selection': choice,
            'artwork_preview_id': transformed['preview_id'], 'confirm_smaller_cover': True})
        assert proposal.status_code == 201, proposal.text
        applied = job(client, headers, 'apply', [identity], {'proposal_id': proposal.json()['id']}); drain(cfg)
        assert client.get('/api/jobs/' + applied['id']).json()['state'] == 'succeeded'
        current = client.get(url).json(); sha = current['sha256']
        assert current['lrc'] == '[00:00.500]Owner reviewed second' and current['preferred_cover']['native_identity'] == 'picture:1'
        # Generic media callers retain the legacy first-front behaviour. The
        # app explicitly asks for its accepted canonical picture instead.
        assert media.read_art(path, cfg.roots)[0] == cover_bytes('red')
        assert media.inspect_audio(path, cfg.roots)['lrc'] == '[00:00.100]First'
        image = client.get(url + '/art')
        assert image.status_code == 200 and hashlib.sha256(image.content).hexdigest() == transformed['result']['sha256']
        job(client, headers, 'audit', [identity]); drain(cfg)
        accept_publication_review(client, headers, identity, profile='lyrics_required', reason='')
        published = job(client, headers, 'publish', [identity]); drain(cfg)
        assert client.get('/api/jobs/' + published['id']).json()['state'] == 'succeeded'
        assert media.sha256_file(cfg.publication_root / 'Ready' / path.name) == sha
        plan = sidecars.create_plan(cfg, [identity], cover=True, lrc=True)
        sources = plan['members'][0]['canonical_sources']
        assert sources['lyrics']['native_identity'] == 'lyrics:1' and sources['cover']['native_identity'] == 'picture:1'
        assert sources['cover']['content_sha256'] == transformed['result']['sha256']
        lyric = next(obj for obj in plan['objects'] if obj['kind'] == 'lrc')
        assert lyric['owners'][identity]['usage_provenance']['lrc']['origin'] == 'owner_supplied'
        sidecars.commit(cfg, plan['id'])
        assert (cfg.publication_root / 'Ready/slots.lrc').read_text() == '[00:00.500]Owner reviewed second\n'
        assert media.sha256_file(cfg.publication_root / 'Ready/cover.jpg') == transformed['result']['sha256']
        exported = exports.generate(cfg)
        manifest = exports.manifest(cfg, exported['export_id'])
        assert manifest['sidecar_files'] == 2
        # A source-pointer change with unchanged audio SHA must still invalidate
        # the frozen source binding, even when the lyric review stays accepted.
        row = db.fetchone(cfg.db_path, 'SELECT metadata FROM tracks WHERE id=?', (identity,))
        changed = json.loads(row['metadata'])
        first = next(slot for slot in changed['slot_inventory']['slots'] if slot['kind'] == 'cover' and slot.get('editable'))
        changed['preferred_cover'] = metadata_slots.reference(first)
        with db.transaction(cfg.db_path) as conn: conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(changed), identity))
        with pytest.raises(media.MediaError) as stale: sidecars.validate_members(cfg, sidecars.load_plan(cfg, plan['id']))
        assert stale.value.code == 'conflict'
        with pytest.raises(media.MediaError) as blocked: exports.generate(cfg)
        assert blocked.value.code == 'sidecar_stale_owner'
        assert media.sha256_file(path) == sha and media.sha256_file(cfg.publication_root / 'Ready' / path.name) == sha
