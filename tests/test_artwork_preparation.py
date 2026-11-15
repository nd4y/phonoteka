"""Lookup hints do not resolve ambiguous editions or re-fetch previous art."""
import copy

import pytest

from phonoteka.artwork_preparation import album_hint

R1 = '11111111-1111-4111-8111-111111111111'
R2 = '22222222-2222-4222-8222-222222222222'


def inputs(album='Album'):
    return ({'has_art': False, 'tags': {'title': ['Song'], 'artist': ['Artist'], 'album': [album]}},
            {'candidates': [], 'sources': []},
            [{'provider': 'musicbrainz', 'kind': 'metadata', 'releases': [
                {'id': R1, 'title': 'Album', 'status': 'Official'},
                {'id': R2, 'title': 'Album', 'status': 'Official'}]}])


def test_known_album_hint_is_not_a_release_selection():
    metadata, analysis, matches = inputs()
    before = copy.deepcopy((metadata, analysis, matches))
    assert album_hint(metadata, analysis, matches) == 'Album'
    assert (metadata, analysis, matches) == before


def test_one_official_album_can_be_queried_without_an_embedded_album():
    assert album_hint(*inputs('')) == 'Album'


def test_known_album_can_narrow_multiple_official_albums_without_choosing_edition():
    metadata, analysis, matches = inputs()
    matches[0]['releases'][1]['title'] = 'Compilation'
    assert album_hint(metadata, analysis, matches) == 'Album'
    metadata['tags']['album'] = []
    assert album_hint(metadata, analysis, matches) is None


@pytest.mark.parametrize('album', ['Album (Deluxe Edition)', 'Album - Single', 'Other album'])
def test_aliases_and_other_editions_are_not_silently_selected(album):
    assert album_hint(*inputs(album)) is None


@pytest.mark.parametrize('state', ['candidate', 'no_match', 'network_error', 'unknown_outcome', 'provider_cooldown'])
def test_any_previous_artwork_response_is_not_retried(state):
    metadata, analysis, matches = inputs()
    analysis['sources'] = [{'provider': 'coverartarchive', 'state': state}]
    assert album_hint(metadata, analysis, matches) is None


@pytest.mark.parametrize('candidate', [{'kind': 'cover'}, {'provider': 'coverartarchive'}, {'cover': 'already present'}])
def test_existing_artwork_candidate_is_reviewed_instead(candidate):
    metadata, analysis, matches = inputs()
    analysis['candidates'] = [candidate]
    assert album_hint(metadata, analysis, matches) is None


@pytest.mark.parametrize('change', ['truncated', 'limit', 'invalid_id', 'empty_title', 'unofficial', 'wrong_provider', 'conflicting_id'])
def test_incomplete_or_unsupported_catalogue_evidence_is_rejected(change):
    metadata, analysis, matches = inputs('')
    if change == 'truncated': matches[0]['releases_truncated'] = True
    elif change == 'limit': matches[0]['releases'] *= 8
    elif change == 'invalid_id': matches[0]['releases'][0]['id'] = 'bad'
    elif change == 'empty_title': matches[0]['releases'][0]['title'] = ' '
    elif change == 'unofficial':
        for r in matches[0]['releases']: r['status'] = 'Bootleg'
    elif change == 'wrong_provider': matches[0]['provider'] = 'shazam'
    else: matches[0]['releases'][1].update(id=R1, title='Other')
    assert album_hint(metadata, analysis, matches) is None


@pytest.mark.parametrize('field', ['possible_mix', 'critical_conflicts', 'unresolved_conflicts'])
def test_conflicts_stay_blocked(field):
    metadata, analysis, matches = inputs()
    analysis[field] = True
    assert album_hint(metadata, analysis, matches) is None


@pytest.mark.parametrize('bad', [None, True, 1, float('nan'), {}, 'bad'])
def test_invalid_list_evidence_is_rejected_without_coercion(bad):
    metadata, analysis, matches = inputs()
    assert album_hint(metadata, analysis, bad) is None
    analysis['sources'] = bad
    assert album_hint(metadata, analysis, matches) is None


def test_missing_identity_and_embedded_art_are_rejected():
    metadata, analysis, matches = inputs()
    metadata['tags']['title'] = ''
    assert album_hint(metadata, analysis, matches) is None
    metadata['tags']['title'] = ['Song']
    metadata['has_art'] = True
    assert album_hint(metadata, analysis, matches) is None


def test_every_matched_recording_needs_complete_official_evidence():
    metadata, analysis, matches = inputs('')
    matches.append({'provider': 'musicbrainz', 'kind': 'metadata', 'releases': []})
    assert album_hint(metadata, analysis, matches) is None
