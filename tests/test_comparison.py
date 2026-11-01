import json
import pytest
from fastapi.testclient import TestClient
from phonoteka import comparison, db, media, reports
from phonoteka.app import create_app
from test_backend import settings, authenticated, seed


def pair(settings):
    left, _ = seed(settings, 'left.mp3')
    right, _ = seed(settings, 'right.mp3')
    with db.transaction(settings.db_path) as conn:
        conn.execute("UPDATE tracks SET metadata=json_set(metadata,'$.duration',30) WHERE id IN (?,?)", (left, right))
    return left, right


def decision(data, mode='preferred', preferred=None):
    return {'ids': [x['id'] for x in data['tracks']], 'revisions': {x['id']: x['sha256'] for x in data['tracks']},
            'version': data['version'], 'supersedes': {x['group_id']:x['version'] for x in data['related_decisions']}, 'mode': mode, 'preferred_id': preferred or data['tracks'][0]['id']}


def test_revision_bound_preference_does_not_change_audio_and_blocks_other_publish(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        ids = pair(settings)
        before = [(p.name, media.sha256_file(p)) for p in settings.work_root.glob('*.mp3')]
        data = client.get('/api/reports/compare', params={'ids': ','.join(ids)}).json()
        assert data['alignment']['reliable'] and data['classification'] == 'exact_bytes'
        body = decision(data)
        saved = client.put('/api/reports/compare/decision', json=body, headers=headers)
        assert saved.status_code == 200 and saved.json()['version'] == 1
        assert client.put('/api/reports/compare/decision', json=body, headers=headers).status_code == 409
        comparison.assert_publish_allowed(settings, ids[0])
        with pytest.raises(media.MediaError, match='другая копия'):
            comparison.assert_publish_allowed(settings, ids[1])
        assert before == [(p.name, media.sha256_file(p)) for p in settings.work_root.glob('*.mp3')]
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('f' * 64, ids[0]))
        assert client.get('/api/reports/compare', params={'ids': ','.join(ids)}).json()['decision_stale']
        with pytest.raises(media.MediaError, match='изменились'):
            comparison.assert_publish_allowed(settings, ids[1])
        assert client.put('/api/reports/compare/decision', json={**body, 'version': 1}, headers=headers).status_code == 412


def test_unverified_cross_codec_candidates_never_get_invented_position_mapping(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings); ids = pair(settings)
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE tracks SET sha256=? WHERE id=?', ('a' * 64, ids[1]))
            for identity in ids:
                row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone()
                meta = json.loads(row['metadata']); meta['tags']['musicbrainz_trackid'] = ['recording-id']
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta), identity))
        data = client.get('/api/reports/compare', params={'ids': ','.join(ids)}).json()
        assert data['classification'] == 'candidates_only' and not data['alignment']['reliable']
        assert data['alignment']['offset_seconds'] is None
        assert reports.summary(settings)['recording_candidates'][0]['verified_duplicate'] is False
        saved = client.put('/api/reports/compare/decision', json=decision(data, 'publish_both'), headers=headers)
        assert saved.status_code == 200
        for identity in ids: comparison.assert_publish_allowed(settings, identity)
        client.post('/api/auth/logout', headers=headers)
        assert client.get('/api/reports/compare', params={'ids': ','.join(ids)}).status_code == 401


def test_explicit_defer_and_csrf_and_missing_track(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings); ids = pair(settings)
        data = comparison.compare(settings, ids)
        assert client.put('/api/reports/compare/decision', json=decision(data)).status_code == 403
        assert client.get('/api/reports/compare', params={'ids': ids[0] + ',absent'}).status_code == 404
        assert client.put('/api/reports/compare/decision', json=decision(data, 'defer'), headers=headers).status_code == 200
        with pytest.raises(media.MediaError, match='отложена'): comparison.assert_publish_allowed(settings, ids[0])


def test_full_group_can_replace_smaller_group_only_with_current_decision_versions(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers=authenticated(client,settings); left,right=pair(settings)
        third,_=seed(settings,'third.mp3')
        initial=comparison.compare(settings,[left,right])
        assert client.put('/api/reports/compare/decision',json=decision(initial),headers=headers).status_code==200
        larger=comparison.compare(settings,[left,right,third]); body=decision(larger,preferred=third)
        assert larger['related_decisions'][0]['version']==1
        assert client.put('/api/reports/compare/decision',json={**body,'supersedes':{}},headers=headers).status_code==409
        assert client.put('/api/reports/compare/decision',json=body,headers=headers).status_code==200
        assert comparison.publication_choice(settings,left)==third
        assert comparison.publication_choice(settings,right)==third
        assert len(db.fetchall(settings.db_path,"SELECT key FROM settings WHERE key LIKE 'duplicate_decision_archive:%'"))==1
