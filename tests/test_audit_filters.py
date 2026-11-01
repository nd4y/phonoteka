"""AUD-05: revision-bound filters and safe root causes agree across API/export."""
import csv
import io
import json

import pytest

from phonoteka import audit_status, db
from test_backend import settings, client, authenticated, seed


SHA = 'a' * 64
OTHER = 'b' * 64


@pytest.mark.parametrize('metadata,status,expected', [
    ({}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA}}, 'needs_review', ('passed', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'warnings': [{'code': 'decoder_diagnostics', 'message': 'Review audio'}]}}, 'needs_review', ('warning', '', '')),
    ({'audit': {'state': 'failed', 'sha256': SHA, 'error': 'invalid_audio', 'failure_stage': 'decode'}}, 'error', ('error', 'invalid_audio', 'decode')),
    ({'audit': {'state': 'unsupported', 'sha256': SHA, 'error': 'verification_too_large', 'failure_stage': 'packets'}}, 'error', ('error', 'verification_too_large', 'packets')),
    ({'audit': {'state': 'failed', 'sha256': OTHER, 'error': 'invalid_audio', 'failure_stage': 'decode'}}, 'error', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': OTHER, 'warnings': [{'code': 'decoder_diagnostics'}]}}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA}, 'input_error': {'code': 'invalid_audio'}}, 'error', ('error', 'invalid_audio', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA}}, 'missing', ('error', 'missing_file', '')),
    ({'format': 'cue', 'audit': {'state': 'failed', 'sha256': SHA}, 'input_error': {'code': 'invalid_audio'}}, 'missing', ('not_applicable', '', '')),
    ({'cue_album_image': True, 'audit': {'state': 'passed', 'sha256': SHA}}, 'needs_review', ('passed', '', '')),
    ({'audit': [{'state': 'passed', 'sha256': SHA}]}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'unknown', 'sha256': SHA, 'error': 'invalid_audio'}}, 'error', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'warnings': None}}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'warnings': {'code': 'decoder_diagnostics'}}}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'warnings': ['private text']}}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'warnings': [{'code': 'private/path'}]}}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'warnings': [{'code': 'decoder_diagnostics', 'message': 17}]}}, 'needs_review', ('unchecked', '', '')),
    ({'audit': {'state': 'failed', 'sha256': SHA, 'error': '/private/path', 'failure_stage': '/private/path'}}, 'error', ('error', 'audit_failed', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'error': 'invalid_audio'}}, 'needs_review', ('error', 'invalid_audio', '')),
    ({'audit': {'state': 'passed', 'sha256': SHA, 'error': '/private/path'}}, 'needs_review', ('unchecked', '', '')),
])
def test_current_revision_policy_rejects_stale_and_malformed_results(metadata, status, expected):
    result = audit_status.describe(metadata, SHA, status)
    assert tuple(result[key] for key in ('audit_status', 'audit_error', 'audit_stage')) == expected
    membership = {name: bool(audit_status.matches(json.dumps(metadata), SHA, status, name)) for name in audit_status.FILTERS}
    assert membership == {'audit_errors': expected[0] == 'error', 'audit_warnings': expected[0] == 'warning',
                          'audit_unchecked': expected[0] == 'unchecked', 'audit_passed': expected[0] in ('passed', 'warning')}


def test_sql_filter_handles_invalid_saved_json_and_unverified_revision_fail_closed():
    assert audit_status.matches('{invalid json', SHA, 'needs_review', 'audit_unchecked') == 1
    assert audit_status.matches('null', SHA, 'needs_review', 'audit_passed') == 0
    assert audit_status.matches('[]', SHA, 'missing', 'audit_errors') == 1
    assert audit_status.describe({'audit': {'state': 'passed', 'sha256': ''}}, '', 'needs_review')['audit_status'] == 'unchecked'


def save_track(settings, name, *, audit=None, status='needs_review', **changes):
    identity, sha = seed(settings, name)
    with db.transaction(settings.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone()
        metadata = json.loads(row['metadata'])
        if audit is not None:
            metadata['audit'] = {**audit, 'sha256': audit.get('sha256', sha)}
        metadata.update(changes)
        conn.execute('UPDATE tracks SET metadata=?,status=? WHERE id=?', (db.encode(metadata), status, identity))
    return identity


def test_audit_filters_share_search_pagination_neighbors_and_public_status(client, settings):
    assert client.get('/api/tracks', params={'filter': 'audit_errors'}).status_code == 401
    authenticated(client, settings)
    passed = save_track(settings, 'set/01.mp3', audit={'state': 'passed'})
    warning = save_track(settings, 'set/02.mp3', audit={'state': 'passed', 'warnings': [{'code': 'decoder_diagnostics'}]})
    failure = save_track(settings, 'set/03.mp3', audit={'state': 'failed', 'error': 'invalid_audio', 'failure_stage': 'decode'})
    stale = save_track(settings, 'set/04.mp3', audit={'state': 'failed', 'sha256': OTHER, 'error': 'invalid_audio'}, status='error')
    unchecked = save_track(settings, 'set/05.mp3')
    input_error = save_track(settings, 'set/06.mp3', input_error={'code': 'invalid_audio'})
    missing = save_track(settings, 'set/07.mp3', status='missing', audit={'state': 'passed'})
    sheet = save_track(settings, 'set/08.cue', format='cue', status='missing', input_error={'code': 'invalid_audio'})
    image = save_track(settings, 'set/09.flac', cue_album_image=True, audit={'state': 'passed'})
    unrelated = save_track(settings, 'elsewhere.mp3', audit={'state': 'passed'})
    expected = {'audit_errors': [failure, input_error, missing], 'audit_warnings': [warning],
                'audit_unchecked': [stale, unchecked], 'audit_passed': [passed, warning, image]}
    for selected, identities in expected.items():
        result = client.get('/api/tracks', params={'filter': selected, 'search': 'set/', 'page_size': 1}).json()
        assert result['total'] == len(identities)
        assert [client.get('/api/tracks', params={'filter': selected, 'search': 'set/', 'page_size': 1, 'page': page}).json()['items'][0]['id']
                for page in range(1, len(identities) + 1)] == identities
    neighbors = client.get(f'/api/tracks/{passed}/neighbors', params={'filter': 'audit_passed', 'search': 'set/'}).json()
    assert neighbors['total'] == 3 and neighbors['next_id'] == warning
    assert client.get(f'/api/tracks/{image}/neighbors', params={'filter': 'audit_passed', 'search': 'set/'}).json()['previous_id'] == warning
    public = {row['id']: row for row in client.get('/api/tracks').json()['items']}
    assert public[failure]['audit_status'] == 'error' and public[failure]['audit_stage'] == 'decode'
    assert public[stale]['audit_status'] == 'unchecked' and public[stale]['audit_error'] == ''
    assert public[sheet]['audit_status'] == 'not_applicable'
    assert public[image]['audit_status'] == public[unrelated]['audit_status'] == 'passed'
    detail = client.get(f'/api/tracks/{warning}').json()
    assert detail['audit_status'] == public[warning]['audit_status'] == 'warning'


def test_ordinary_catalog_queries_do_not_run_the_audit_sql_function(client, settings, monkeypatch):
    authenticated(client, settings)
    save_track(settings, 'ordinary.mp3')
    calls = []
    original = audit_status.matches
    def watched(*args):
        calls.append(args[-1])
        return original(*args)
    monkeypatch.setattr(audit_status, 'matches', watched)
    assert client.get('/api/tracks').status_code == 200
    assert client.get('/api/tracks', params={'search': 'ordinary', 'filter': 'missing_art'}).status_code == 200
    assert calls == []
    assert client.get('/api/tracks', params={'filter': 'audit_unchecked'}).json()['total'] == 1
    assert calls and set(calls) == {'audit_unchecked'}


def test_exports_keep_legacy_state_but_add_only_current_safe_root_causes(client, settings):
    authenticated(client, settings)
    failed = save_track(settings, '01.mp3', audit={'state': 'failed', 'error': 'invalid_audio', 'failure_stage': 'decode',
                        'message': 'private decoder text', 'stderr': 'private raw stderr'})
    limited = save_track(settings, '02.wav', audit={'state': 'failed', 'error': 'verification_too_large', 'failure_stage': 'packets'})
    stale = save_track(settings, '03.mp3', audit={'state': 'failed', 'sha256': OTHER, 'error': 'invalid_audio', 'failure_stage': 'decode'})
    malformed = save_track(settings, '04.mp3', audit={'state': 'passed', 'warnings': ['private malformed warning']})
    input_error = save_track(settings, '05.mp3', input_error={'code': 'invalid_audio', 'message': 'private input message'})
    missing = save_track(settings, '06.mp3', status='missing')
    csv_response = client.get('/api/reports/export/csv')
    assert csv_response.status_code == 200
    records = {row['id']: row for row in csv.DictReader(io.StringIO(csv_response.text))}
    assert (records[failed]['audit_state'], records[failed]['audit_status'], records[failed]['audit_error'], records[failed]['audit_stage']) == ('failed', 'error', 'invalid_audio', 'decode')
    assert records[limited]['audit_error'] == 'verification_too_large' and records[limited]['audit_stage'] == 'packets'
    assert records[stale]['audit_state'] == 'not_current' and records[stale]['audit_status'] == 'unchecked'
    assert records[stale]['audit_error'] == records[stale]['audit_stage'] == ''
    assert records[malformed]['audit_state'] == 'passed' and records[malformed]['audit_status'] == 'unchecked'
    assert records[input_error]['audit_error'] == 'invalid_audio' and records[input_error]['audit_stage'] == ''
    assert records[missing]['audit_error'] == 'missing_file'
    json_response = client.get('/api/reports/export/json')
    assert json_response.status_code == 200
    assert {row['id']: row['audit_status'] for row in json_response.json()['files']} == {identity: row['audit_status'] for identity, row in records.items()}
    for response in (csv_response, json_response):
        assert 'private decoder text' not in response.text and 'private raw stderr' not in response.text
        assert 'private malformed warning' not in response.text and 'private input message' not in response.text


def test_owner_exports_tolerate_malformed_legacy_audit_and_input_error_without_approving_publication(client, settings):
    authenticated(client, settings)
    cases = [('none', None, None, 'unchecked', ''),
             ('list', [{'state': 'passed'}], None, 'unchecked', ''),
             ('input-string', None, 'private legacy input text', 'error', 'input_error'),
             ('input-list', None, ['private legacy input list'], 'error', 'input_error')]
    expected = {}
    fallback_identity = 'f' * 64
    for name, audit_value, input_error_value, state, code in cases:
        identity = save_track(settings, name + '.mp3')
        with db.transaction(settings.db_path) as conn:
            row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone()
            metadata = json.loads(row['metadata'])
            metadata.update(audit=audit_value, input_error=input_error_value, audio_identity=fallback_identity)
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(metadata), identity))
        expected[identity] = (state, code)

    csv_response = client.get('/api/reports/export/csv')
    json_response = client.get('/api/reports/export/json')
    assert csv_response.status_code == json_response.status_code == 200
    csv_records = {row['id']: row for row in csv.DictReader(io.StringIO(csv_response.text))}
    payload = json_response.json()
    json_records = {row['id']: row for row in payload['files']}
    assert set(csv_records) == set(json_records) == set(expected)
    for identity, (state, code) in expected.items():
        for records in (csv_records, json_records):
            row = records[identity]
            assert (row['audit_status'], row['audit_error'], row['audit_stage']) == (state, code, '')
            assert row['error'] == ''
            assert row['publication_gate'] != 'passed'
            assert 'current_decode_audit_required' in row['publication_reasons']
    summary = payload['summary']
    assert summary['counts']['input_errors'] == 2
    assert summary['counts']['files'] == 4
    assert len(summary['same_audio_packets']) == 1
    assert {row['id'] for row in summary['same_audio_packets'][0]} == set(expected)
    for response in (csv_response, json_response):
        assert 'private legacy input text' not in response.text and 'private legacy input list' not in response.text
