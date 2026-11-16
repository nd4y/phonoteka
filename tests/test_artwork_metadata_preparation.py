"""Native UI-plan/queue contracts for a separate artwork-only lookup capability."""
import copy
import json
import time
import uuid

import pytest

from phonoteka import db, media, metadata_preparation as prep, provider_policy
from phonoteka.providers import Providers
from test_backend import drain
from test_confirmed_fields import library
from test_metadata_preparation import entry, settings, start


def setup_artwork(cfg, tid):
    with db.transaction(cfg.db_path) as conn:
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        meta = json.loads(row['metadata'])
        meta['tags']['album'] = ['Confirmed album']
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), tid))
        analysis = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()[0])
        complete = copy.deepcopy(analysis)
        analysis['candidates'] = [c for c in analysis['candidates'] if c.get('provider') != 'coverartarchive']
        analysis['sources'] = []
        analysis['query'] = {'title': 'Original', 'artist': 'Artist', 'album': ''}
        from test_provider_policy import grant
        grant(conn, 'lrclib', 'lyrics')
        retained = {'id': 'lrclib:retained', 'provider': 'lrclib', 'kind': 'lyrics',
                    'plain_lyrics': 'Existing draft must remain unchanged',
                    'match': {'state': 'matching_metadata'}}
        retained['usage'] = provider_policy.candidate_usage(conn, retained, now=time.time() - 30)
        analysis['candidates'].append(retained)
        analysis['sources'].append({'provider': 'lrclib', 'state': 'candidate'})
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(analysis), tid))
        row = conn.execute('SELECT * FROM tracks WHERE id=?', (tid,)).fetchone()
        old_key = 'metadata_prepared:' + prep.fields.digest([prep.CONTRACT, tid, row['sha256'], 'analyze', prep.guards(conn, row)])
        old_value = db.encode({'run_id': str(uuid.uuid4())})
        conn.execute('INSERT INTO settings VALUES(?,?)', (old_key, old_value))
    return complete, old_key, old_value


def test_known_album_becomes_analyze_only_without_reopening_old_receipt(library, monkeypatch):
    cfg, client, headers, tid, sha, path = library
    complete, old_key, old_value = setup_artwork(cfg, tid)
    with db.connect(cfg.db_path) as conn:
        old_analysis = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()[0])
        old_lyrics = next(c for c in old_analysis['candidates'] if c['id'] == 'lrclib:retained')
    before = media.sha256_file(path)
    value = entry(client)
    assert not value['ready'] and not value['changes']
    assert any(g['code'] == 'cover_evidence_missing' for g in value['remaining'])
    assert value['preparation']['kind'] == 'analyze'
    assert value['preparation']['album_hint'] == 'Confirmed album'
    assert value['preparation']['scan'] is False
    calls = []

    async def analyze(self, metadata, relative_path, identified, authorized, **options):
        authorized()
        calls.append(options)
        assert options == {'album_hint': 'Confirmed album'}
        return copy.deepcopy(complete)

    monkeypatch.setattr(Providers, 'analyze', analyze)
    accepted = start(client, headers)
    assert accepted.status_code == 202, accepted.text
    run = accepted.json()
    assert run['stage'] == 'search'
    with db.connect(cfg.db_path) as conn:
        job = conn.execute('SELECT kind,payload FROM jobs WHERE id=?', (run['job_id'],)).fetchone()
        assert job['kind'] == 'analyze'
        item, = json.loads(job['payload'])['items']
        assert item['album_hint'] == 'Confirmed album' and item['scan'] is False
    drain(cfg)
    prep.tick(cfg)
    assert calls == [{'album_hint': 'Confirmed album'}]
    assert client.get('/api/metadata-preparation').json()['state'] == 'review'
    assert media.sha256_file(path) == before == sha
    assert db.setting(cfg.db_path, old_key) == old_value
    with db.connect(cfg.db_path) as conn:
        result = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()[0])
        retained = next(c for c in result['candidates'] if c['id'] == 'lrclib:retained')
        assert retained['plain_lyrics'] == 'Existing draft must remain unchanged'
        assert retained['usage'] == old_lyrics['usage']
        assert result['asr'] == complete['asr'] and result['alignment'] == complete['alignment']
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind IN ('recognize','apply','publish')").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM settings WHERE key GLOB 'recognition_usage:*' OR key GLOB 'recognition_attempt:*'").fetchone()[0] == 0


def test_failed_or_cancelled_album_lookup_is_not_automatically_replayed(library):
    cfg, client, headers, tid, sha, path = library
    _, old_key, old_value = setup_artwork(cfg, tid)
    before = entry(client)['preparation']
    accepted = start(client, headers).json()
    assert client.post('/api/metadata-preparation/cancel', json={'id': accepted['id']}, headers=headers).status_code == 200
    assert entry(client)['preparation'] is None
    assert db.setting(cfg.db_path, old_key) == old_value
    with db.connect(cfg.db_path) as conn:
        assert conn.execute('SELECT 1 FROM settings WHERE key=?', ('metadata_prepared:' + before['receipt'],)).fetchone()
    assert media.sha256_file(path) == sha


@pytest.mark.parametrize('state', ['no_match', 'network_error', 'unknown_outcome', 'provider_cooldown'])
def test_existing_artwork_outcome_requires_review_instead_of_new_capability(library, state):
    cfg, client, headers, tid, sha, path = library
    setup_artwork(cfg, tid)
    with db.transaction(cfg.db_path) as conn:
        data = json.loads(conn.execute('SELECT payload FROM analyses WHERE track_id=?', (tid,)).fetchone()[0])
        data['sources'].append({'provider': 'coverartarchive', 'state': state})
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?', (db.encode(data), tid))
    assert entry(client)['preparation'] is None


def test_changed_file_and_forged_hint_cannot_change_the_frozen_run(library):
    cfg, client, headers, tid, sha, path = library
    setup_artwork(cfg, tid)
    accepted = start(client, headers).json()
    with db.transaction(cfg.db_path) as conn:
        run = prep.load(conn)
        job = dict(conn.execute('SELECT * FROM jobs WHERE id=?', (accepted['job_id'],)).fetchone())
        payload = json.loads(job['payload'])
        payload['items'][0]['album_hint'] = 'Other album'
        job['payload'] = db.encode(payload)
        with pytest.raises(media.MediaError) as error:
            prep.validate(conn, cfg, run, job)
        assert error.value.code == 'preparation_invalid'
        conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('a' * 64, tid))
        with pytest.raises(media.MediaError) as error:
            prep.validate(conn, cfg, run)
        assert error.value.code == 'preparation_stale'
    assert media.sha256_file(path) == sha


def test_newer_failed_search_is_not_replayed_as_an_artwork_capability(library):
    from test_enrichment_gaps import failed_search
    cfg, client, headers, tid, sha, path = library
    setup_artwork(cfg, tid)
    failed_search(cfg, tid, sha)
    assert entry(client)['preparation'] is None


def test_missing_but_locked_cover_is_reported_and_not_prepared(library):
    from test_field_policy import locks
    cfg, client, headers, tid, sha, path = library
    setup_artwork(cfg, tid)
    response = locks(client, headers, tid, sha, ['cover'])
    assert response.status_code == 200, response.text
    value = entry(client)
    assert any(g['field'] == 'cover' and g['code'] == 'field_locked' for g in value['remaining'])
    assert value['preparation'] is None


@pytest.mark.parametrize('field', ['cover', 'album'])
def test_pending_edition_or_artwork_is_not_replaced_by_album_capability(library, field):
    cfg, client, headers, tid, sha, path = library
    complete, _, _ = setup_artwork(cfg, tid)
    cover = next(c['cover'] for c in complete['candidates'] if c.get('cover'))
    payload = {'cover': cover} if field == 'cover' else {'tags': {'album': 'Different album'}}
    with db.transaction(cfg.db_path) as conn:
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', (str(uuid.uuid4()), tid, sha, db.encode(payload), db.utcnow()))
    value = entry(client)['preparation']
    assert value is None or not value.get('album_hint')
    assert media.sha256_file(path) == sha
