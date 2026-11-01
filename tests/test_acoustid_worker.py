import asyncio
from dataclasses import replace
import json

import pytest

from phonoteka import acoustid, db, media, recognition
from phonoteka.worker import Worker
from test_backend import client, settings, seed
from test_acoustid import CLIENT, result


@pytest.mark.parametrize('state', ['no_match', 'ambiguous', 'matched', 'mix', 'unknown_outcome'])
def test_worker_fallback_and_metadata_refresh_preserve_evidence_without_file_writes(client, settings, monkeypatch, state):
    cfg = replace(settings, external_fingerprints=True, external_metadata=True)
    acoustid.save_connection(cfg, 'initial', True, CLIENT)
    identity, sha = seed(cfg)
    with db.transaction(cfg.db_path) as conn:
        row = conn.execute('SELECT metadata FROM tracks WHERE id=?', (identity,)).fetchone()
        meta = json.loads(row['metadata'])
        meta.update(duration=240, has_art=True, audit={'state':'passed','sha256':sha,
            'profile':'full_decode_all_audio_streams','audio_identity':'b'*64})
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),identity))
    shazam = {'id':'shazam:known','provider':'shazam','kind':'metadata','tags':{'title':'Shazam song','artist':'Artist'},
        'identification':'audio_fingerprint','provider_recording_id':'known'}
    async def segments(*args, **kwargs):
        return {'possible_mix':state=='mix','state':state,'segments':[
            {'state':'match' if state=='matched' else 'no_match' if state in {'no_match','ambiguous','mix'} else state,
                'candidate':shazam if state=='matched' else None,'offset':0,'sample_sha256':'c'*64}]}
    monkeypatch.setattr(recognition, 'identify_segments', segments)
    calls=[]; queries=[]
    monkeypatch.setattr(acoustid,'fingerprint',lambda *args: (calls.append('fingerprint') or 'AQAAA_fixture_signature'))
    async def identify(*args, **kwargs):
        calls.append('lookup')
        candidates=acoustid.candidates(result()['results'],240)
        if state=='ambiguous':candidates.append({**candidates[0],'id':'acoustid:other','recording_id':'00000000-0000-0000-0000-000000000000'})
        return candidates
    monkeypatch.setattr(acoustid,'identify',identify)
    class Fixture:
        async def analyze(self, meta, relative, identified, authorize):
            queries.append(identified)
            return {'input_sha256':sha,'state':'no_match','candidates':[],'sources':[]}
    task={'id':'fixture','kind':'recognize','auth_epoch':int(db.setting(cfg.db_path,'auth_epoch','0'))}
    item={'track_id':identity,'sha256':sha,'relative_path':'a.mp3'}
    worker=Worker(cfg); worker.providers=Fixture(); worker.cancelled=lambda _:False
    monkeypatch.setattr(worker,'item_action',lambda *args:None)
    try:
        asyncio.run(worker.analyze_item(task,item))
        payload=json.loads(db.fetchone(cfg.db_path,'SELECT payload FROM analyses WHERE track_id=?',(identity,))['payload'])
        if state in {'no_match','ambiguous'}:
            assert calls==['fingerprint','lookup']
            assert queries[0] is None if state=='ambiguous' else queries[0]['provider']=='acoustid'
            assert any(c['provider']=='acoustid' for c in payload['candidates'])
            assert any(s['provider']=='acoustid' and s['state']=='fingerprint_match' for s in payload['sources'])
            asyncio.run(worker.analyze_item({**task,'kind':'analyze'},item))
            refreshed=json.loads(db.fetchone(cfg.db_path,'SELECT payload FROM analyses WHERE track_id=?',(identity,))['payload'])
            assert any(c['provider']=='acoustid' for c in refreshed['candidates'])
            assert any(s['provider']=='acoustid' for s in refreshed['sources'])
            assert calls==['fingerprint','lookup']
            if state=='ambiguous':assert queries[-1] is None
        else:
            assert calls==[]
        assert media.sha256_file(cfg.work_root/'a.mp3')==sha
    finally:
        worker.pool.shutdown()
