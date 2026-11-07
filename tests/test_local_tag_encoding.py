import json

import pytest

from phonoteka import db, media, provider_policy as policy
from test_backend import authenticated, client, settings, seed


def local_row(settings, damaged, provenance=None):
    track_id, _ = seed(settings)
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (track_id,)).fetchone()
        meta = json.loads(row['metadata'])
        meta['tags']['title'] = damaged if isinstance(damaged, list) else [damaged]
        if provenance:
            meta['usage_provenance'] = {'title': provenance}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), track_id))
    return dict(db.fetchone(settings.db_path, 'SELECT * FROM tracks WHERE id=?', (track_id,)))


def old_suggestion(conn, row, title):
    candidate = {'id': 'shazam:old', 'provider': 'shazam', 'kind': 'metadata', 'tags': {'title': title}}
    usage = policy.candidate_usage(conn, candidate)
    policy.register_values(conn, candidate, usage)
    payload = {'tags': {'title': title}, 'field_sources': {'title': {'usage': usage}}}
    conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('old', row['id'], row['sha256'], db.encode(payload), db.utcnow()))
    return usage


@pytest.mark.parametrize('steps', [
    [('utf-8', 'latin1')], [('cp1251', 'latin1')],
    [('cp1251', 'latin1'), ('utf-8', 'latin1')], [('utf-8', 'cp1251')],
])
def test_file_encoding_repair_does_not_inherit_unapplied_provider_suggestion(client, settings, steps):
    title = 'Привет'
    damaged = title
    for encoding, decoding in steps:
        damaged = damaged.encode(encoding).decode(decoding)
    row = local_row(settings, damaged)
    with db.transaction(settings.db_path) as conn:
        old_suggestion(conn, row, title)
    headers = {**authenticated(client, settings), 'If-Match': row['sha256']}
    response = client.post(f"/api/tracks/{row['id']}/proposal", headers=headers, json={'tags': {'title': title}})
    assert response.status_code == 201, response.text
    saved = json.loads(db.fetchone(settings.db_path, 'SELECT payload FROM proposals WHERE id=?', (response.json()['id'],))['payload'])
    with db.connect(settings.db_path) as conn:
        checked = policy.require_payload(conn, row, saved, 'embed')
        assert not policy.rule(conn, 'shazam', 'metadata')['allow_cache']
    source = checked['field_sources']['title']
    assert source['origin'] == 'preexisting' and source['license_verified'] is False
    proof = source['local_transform']
    assert proof['input_sha256'] == row['sha256']
    assert proof['source_value_sha256'] == policy.digest([damaged])
    assert len(proof['steps']) in {1, 2}


@pytest.mark.parametrize('restriction', ['acquired', 'inherited'])
def test_encoding_repair_preserves_restrictions_on_embedded_data(client, settings, restriction):
    title = 'Привет'
    with db.connect(settings.db_path) as conn:
        usage = policy.candidate_usage(conn, {'provider': 'shazam', 'kind': 'metadata'})
    if restriction == 'inherited':
        usage = {'origin': 'owner_supplied', 'provider': 'local', 'inherited_restrictions': [usage]}
    row = local_row(settings, title.encode().decode('latin1'), {'usage': usage})
    with db.connect(settings.db_path) as conn:
        with pytest.raises(media.MediaError) as error:
            policy.require_payload(conn, row, {'tags': {'title': title}}, 'cache')
        assert error.value.code == 'provider_policy_denied'


@pytest.mark.parametrize('original', [['Example'], ['Привет'.encode().decode('latin1'), 'Another artist']])
def test_non_encoding_edits_and_multivalued_fields_keep_manual_policy(client, settings, original):
    row = local_row(settings, original)
    with db.transaction(settings.db_path) as conn:
        old_suggestion(conn, row, 'Привет')
        with pytest.raises(media.MediaError):
            policy.require_payload(conn, row, {'tags': {'title': 'Привет'}}, 'cache')


def test_explicit_provider_source_is_not_replaced_by_encoding_proof(client, settings):
    row = local_row(settings, 'Привет'.encode().decode('latin1'))
    with db.transaction(settings.db_path) as conn:
        usage = old_suggestion(conn, row, 'Привет')
        with pytest.raises(media.MediaError):
            policy.require_payload(conn, row, {'tags': {'title': 'Привет'}, 'field_sources': {'title': {'usage': usage}}}, 'cache')


def test_encoding_repair_still_requires_current_file_revision(client, settings):
    row = local_row(settings, 'Привет'.encode().decode('latin1'))
    headers = {**authenticated(client, settings), 'If-Match': '0' * 64}
    response = client.post(f"/api/tracks/{row['id']}/proposal", headers=headers, json={'tags': {'title': 'Привет'}})
    assert response.status_code == 412
    assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM proposals')['n'] == 0
