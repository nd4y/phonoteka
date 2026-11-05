"""Alignment admission checks the actual words, not unrelated album fields."""
from dataclasses import replace
import json
import time

from fastapi.testclient import TestClient
import pytest

from phonoteka import db, provider_policy as policy
from phonoteka.app import create_app
from test_backend import settings, authenticated, seed
from test_provider_policy import grant, lyrics


@pytest.mark.parametrize('pending,expired,revoked,expected', [
    (False, True, False, 202),
    (False, False, True, 409),
    (True, False, False, 202),
    (True, True, False, 409),
    (True, False, True, 409),
])
def test_actual_reference_policy_survives_admission_and_queue_cleanup(settings, pending, expired, revoked, expected):
    cfg = replace(settings, inference_url='http://inference:8001')
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        words = lyrics()['lrc']
        track_id, sha = seed(cfg, lrc='' if pending else words)
        with db.transaction(cfg.db_path) as conn:
            grant(conn)
            usage = policy.candidate_usage(conn, lyrics(), now=time.time() - (7200 if expired else 0))
            unrelated = policy.candidate_usage(conn, {'provider':'musicbrainz','kind':'metadata'}, now=time.time()-30*86400)
            fields = {'title': {'usage': unrelated}, 'lrc': {'usage': usage}}
            row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (track_id,)).fetchone()
            meta = json.loads(row['metadata'])
            if pending:
                proposal = {'tags':{'title':'Old unrelated tag'}, 'lrc':words, 'field_sources':fields,
                            'sources':[{'kind':'metadata','usage':unrelated}, {'kind':'lyrics','usage':usage}]}
                conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('pending',track_id,sha,db.encode(proposal),db.utcnow()))
            else:
                meta['usage_provenance'] = fields
                conn.execute('UPDATE tracks SET metadata=? WHERE id=?', (db.encode(meta),track_id))
            if revoked:
                grant(conn, allow_derived_analysis=False)
        response = client.post('/api/jobs', headers={**headers,'Idempotency-Key':'align-reference'},
                               json={'kind':'align','track_ids':[track_id]})
        assert response.status_code == expected, response.text
        if expected == 409:
            assert response.json()['code'] == 'provider_policy_denied'
            assert db.fetchone(cfg.db_path,'SELECT COUNT(*) AS n FROM jobs')['n'] == 0
            return
        identity = response.json()['id']
        queued = db.fetchone(cfg.db_path,'SELECT payload FROM jobs WHERE id=?',(identity,))
        item = json.loads(queued['payload'])['items'][0]
        assert set(item['reference_sources']) == {'lrc'}
        policy.cleanup_cache(cfg)
        after = db.fetchone(cfg.db_path,'SELECT state,payload FROM jobs WHERE id=?',(identity,))
        assert after['state'] == 'queued'
        assert json.loads(after['payload'])['items'][0]['reference_text']
        # The worker repeats this check before submitting to the ASR service.
        with db.connect(cfg.db_path) as conn:
            row = conn.execute('SELECT * FROM tracks WHERE id=?',(track_id,)).fetchone()
            policy.require_payload(conn,row,{'lrc':item['reference_text'],'field_sources':item['reference_sources']},'derived_analysis')


def test_proposal_source_only_constraints_are_frozen_for_worker(settings):
    cfg = replace(settings,inference_url='http://inference:8001')
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers = authenticated(client,cfg)
        track_id,sha = seed(cfg)
        with db.transaction(cfg.db_path) as conn:
            grant(conn)
            usage = policy.candidate_usage(conn,lyrics())
            proposal = {'lrc':lyrics()['lrc'],'sources':[{'kind':'lyrics','usage':usage}]}
            conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',('source-only',track_id,sha,db.encode(proposal),db.utcnow()))
        response = client.post('/api/jobs',headers={**headers,'Idempotency-Key':'source-only'},json={'kind':'align','track_ids':[track_id]})
        assert response.status_code == 202,response.text
        item = json.loads(db.fetchone(cfg.db_path,'SELECT payload FROM jobs WHERE id=?',(response.json()['id'],))['payload'])['items'][0]
        assert any(source.get('provider')=='lrclib' for source in item['reference_sources']['lrc']['usage'])
        with db.transaction(cfg.db_path) as conn:
            grant(conn,allow_cache=False)
        policy.cleanup_cache(cfg)
        assert client.get('/api/jobs/'+response.json()['id']).json()['state'] == 'cancelled'
