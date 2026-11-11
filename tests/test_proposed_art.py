"""The saved preview must return the exact proposal's image, never current audio art."""
import base64
import hashlib
import io
import uuid

from PIL import Image

from phonoteka import db, provider_policy
from test_backend import authenticated, client, settings, seed
from test_provider_policy import grant


def save_proposed_art(settings, track_id, sha, *, color='red', cover=True, source=False):
    stream = io.BytesIO()
    Image.new('RGB', (16, 16), color=color).save(stream, 'PNG')
    raw = stream.getvalue()
    value = 'data:image/png;base64,' + base64.b64encode(raw).decode()
    payload = {'tags': {}, **({'cover': value} if cover else {})}
    identity = str(uuid.uuid4())
    with db.transaction(settings.db_path) as conn:
        if source:
            grant(conn, 'coverartarchive', 'cover')
            candidate = {'id': 'caa:test', 'provider': 'coverartarchive', 'kind': 'cover', 'cover': value}
            usage = provider_policy.candidate_usage(conn, candidate)
            payload['field_sources'] = {'cover': {'usage': usage}}
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',
                     (identity, track_id, sha, db.encode(payload), db.utcnow()))
    return identity, raw


def test_saved_art_is_exact_private_read_only_and_not_the_latest_proposal(client, settings):
    track_id, sha = seed(settings)
    first, raw = save_proposed_art(settings, track_id, sha)
    second, newer = save_proposed_art(settings, track_id, sha, color='blue')
    path = f'/api/tracks/{track_id}/proposals/{first}/art'
    assert client.get(path).status_code == 401
    authenticated(client, settings)
    response = client.get(path)
    assert response.status_code == 200 and response.content == raw and raw != newer
    assert response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'] == 'private, no-store'
    assert client.get(f'/api/tracks/{track_id}/proposals/{second}/art').content == newer
    assert hashlib.sha256((settings.work_root/'a.mp3').read_bytes()).hexdigest() == sha
    with db.connect(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM proposals').fetchone()[0] == 2


def test_saved_art_rejects_other_track_old_revision_and_missing_cover(client, settings):
    authenticated(client, settings)
    track_id, sha = seed(settings)
    other_id, _ = seed(settings, 'other.mp3')
    identity, _ = save_proposed_art(settings, track_id, sha)
    assert client.get(f'/api/tracks/{other_id}/proposals/{identity}/art').status_code == 404
    empty, _ = save_proposed_art(settings, track_id, sha, cover=False)
    assert client.get(f'/api/tracks/{track_id}/proposals/{empty}/art').status_code == 404
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('b'*64, track_id))
    assert client.get(f'/api/tracks/{track_id}/proposals/{identity}/art').status_code == 412


def test_saved_art_honors_revoked_cache_policy(client, settings):
    authenticated(client, settings)
    track_id, sha = seed(settings)
    identity, _ = save_proposed_art(settings, track_id, sha, source=True)
    path = f'/api/tracks/{track_id}/proposals/{identity}/art'
    assert client.get(path).status_code == 200
    with db.transaction(settings.db_path) as conn:
        grant(conn, 'coverartarchive', 'cover', allow_cache=False)
    response = client.get(path)
    assert response.status_code == 409 and response.json()['code'] == 'provider_policy_denied'
