"""A featured artist can be credited in either field, but may never be discarded."""
from dataclasses import replace
import asyncio
import json

import pytest

from phonoteka import collection_fill, db, media, preparation
from phonoteka.providers import CREDIT_QUERY_PROFILE, LYRICS_QUERY_PROFILE, Providers, featured_credit, lyric_candidate, lyric_search_profile
from test_audio_check import report, settings as audio_settings
from test_confirmed_fields import library


@pytest.fixture
def settings(tmp_path):
    return replace(audio_settings.__wrapped__(tmp_path), external_metadata=True)


QUERY = {'title': 'Track (feat. Guest)', 'artist': 'Lead', 'album': 'Album'}


@pytest.mark.parametrize(('title', 'artist', 'duration', 'matches'), [
    ('Track', 'Lead featuring Guest', 100, True),
    ('Track [ft. Guest]', 'Lead', 100, True),
    ('Track', 'Lead', 100, False),
    ('Track', 'Lead feat. Other', 100, False),
    ('Track', 'Guest feat. Lead', 100, False),
    ('Track (live)', 'Lead feat. Guest', 100, False),
    ('Track', 'Lead feat. Guest', 114, False),
])
def test_qualified_matching_preserves_performers_edition_and_audio_gate(title, artist, duration, matches):
    result = lyric_candidate({'trackName': title, 'artistName': artist, 'duration': duration,
                              'id': 12, 'syncedLyrics': '[00:01]First'}, QUERY, 100)
    assert (result['match']['state'] == 'matching_metadata') is matches
    assert result['timing_verified'] is False


def test_only_unambiguous_embedded_credits_get_the_upgrade():
    assert lyric_search_profile({'title': ['Track (feat. Guest)'], 'artist': ['Lead']}) == CREDIT_QUERY_PROFILE
    assert lyric_search_profile({'title': ['Track (feat. Guest)'], 'artist': ['Lead', 'Other']}) == LYRICS_QUERY_PROFILE
    assert featured_credit({'title': 'Track (feat. Guest)', 'artist': 'Lead ft. Other'}) is None
    assert featured_credit({'title': 'Track feat. Guest ft. Other', 'artist': 'Lead'}) is None
    assert featured_credit({'title': 'Track (live)', 'artist': 'Lead'}) is None


def test_upgrade_makes_one_distinct_named_request(settings):
    calls = []
    async def run():
        provider = Providers(settings)
        async def get(service, url, params):
            calls.append((service, url, params))
            return [{'trackName': 'Track', 'artistName': 'Lead ft. Guest', 'duration': 100,
                     'id': 1, 'syncedLyrics': '[00:01]First'}]
        provider.get = get
        try:
            result, = await provider.lyrics(QUERY, 100, expanded_only=True)
            assert result['match']['state'] == 'matching_metadata'
        finally:
            await provider.close()
    asyncio.run(run())
    assert calls == [('lrclib', 'https://lrclib.net/api/search', {'track_name': 'Track', 'artist_name': 'Lead'})]


def test_old_supplement_exposes_one_scoped_continuation(report):
    cfg, client, headers, tid, sha, path, url = report
    with db.transaction(cfg.db_path) as conn:
        run = preparation.load(conn)
        run['supplements'] = run.pop('refines')
        run.update(autofill=True, scanned_files=1, scan_errors=0, profile='lyrics_required', folder='')
        preparation.save(conn, run)
        assert preparation.public(run)['can_refine_lyrics']
    endpoint = url.rsplit('/', 1)[0] + '/refine-lyrics'
    started = client.post(endpoint, headers=headers)
    assert started.status_code == 202, started.text
    value = started.json()
    assert value['credit_only'] and value['scan_id'] == run['scan_id']
    assert client.post(endpoint, headers=headers).json()['id'] == value['id']
    assert not preparation.public({**value, 'state': 'review'})['can_refine_lyrics']
    assert client.post('/api/preparation/' + value['id'] + '/supplement', headers=headers).status_code == 409
    assert media.sha256_file(path) == sha



def test_credit_search_hands_new_timestamped_result_to_audio_verification(report):
    cfg, _, _, tid, sha, path, _ = report
    with db.transaction(cfg.db_path) as conn:
        run = preparation.load(conn)
        metadata = json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()[0])
        metadata['tags'].update(title=[QUERY['title']], artist=[QUERY['artist']])
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), tid))
        run.update(autofill=True, scanned_files=1, scan_errors=0, processed=0, state='active', credit_only=True,
                   profile='lyrics_required', epoch=int(db.setting(cfg.db_path, 'auth_epoch')), counts={}, reasons={},
                   page=[{'id': tid, 'sha': sha, 'tried': ['previous-lyric-search']}])
        collection_fill.advance(conn, cfg, run)
        assert run['action'] == 'transcribe'
        item, = json.loads(conn.execute('SELECT payload FROM jobs WHERE id=?', (run['job_id'],)).fetchone()[0])['items']
        assert item['track_id'] == tid and item['sha256'] == sha and item['language_hint'] == 'ru'
        assert 'reference_text' not in item
    assert media.sha256_file(path) == sha
