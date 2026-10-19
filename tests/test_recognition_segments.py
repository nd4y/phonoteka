"""Real worker/API policy with local mocked provider results, never real HTTP."""
import asyncio
from dataclasses import replace
import hashlib
import json
import sys

import pytest
from fastapi.testclient import TestClient

from phonoteka import db, media, recognition, provider_attempts
from phonoteka.providers import ProviderError
from phonoteka.app import create_app
from test_backend import settings, authenticated, seed, job, drain


def candidate(identity):
    return {'id':'shazam:'+identity,'provider':'shazam','provider_recording_id':identity,'kind':'metadata',
            'tags':{'title':'Song '+identity,'artist':'Artist'},'identification':'audio_fingerprint',
            'match':{'state':'fingerprint_match','reasons':['recording_version_unverified']}}


@pytest.mark.parametrize('answers,expected', [(['one','one'],'agreement'),(['one','two'],'conflict'),
                                              ([None,'one'],'insufficient'),([None,None],'insufficient')])
def test_second_fragment_even_after_match_and_recording_only_verdict(monkeypatch,answers,expected):
    sampled=[];sent=[]
    async def sample(offset):sampled.append(offset);return str(offset).encode(),offset
    async def identify(providers,audio,offset,authorize,*,input_revision):
        assert provider_attempts.RECOGNITION.get()[0]=='b'*64
        sent.append(offset)
        answer=answers[len(sent)-1]
        return candidate(answer) if answer else None
    monkeypatch.setattr(recognition,'recognize',identify)
    result=asyncio.run(recognition.identify_segments(None,sample,120,lambda:None,
        input_revision='a'*64,audio_identity='b'*64,batch_id='fixture'))
    assert len(sent)==len(sampled)==2 and sent[0]!=sent[1]
    assert result['state']==expected and result['possible_mix']==(expected=='conflict')
    assert result['recording_only'] and not result['release_verified'] and not result['lyrics_verified']
    assert all(entry['sample_sha256']==hashlib.sha256(str(entry['offset']).encode()).hexdigest() for entry in result['segments'])


def test_cancel_between_fragments_stops_before_sampling_or_next_send(monkeypatch):
    calls=[];checks=[]
    def authorize():
        checks.append(1)
        if len(checks)==4:raise media.MediaError('cancelled','Synthetic cancellation')
    async def sample(offset):return b'sample',offset
    async def identify(*args,**kwargs):calls.append(1);return candidate('one')
    monkeypatch.setattr(recognition,'recognize',identify)
    with pytest.raises(media.MediaError) as error:
        asyncio.run(recognition.identify_segments(None,sample,120,authorize,
            input_revision='a'*64,audio_identity='b'*64,batch_id='fixture'))
    assert error.value.code=='cancelled' and len(calls)==1


def test_segments_share_existing_lifetime_budget_no_new_batch_allowance(settings,monkeypatch):
    db.initialize(settings.db_path)
    with provider_attempts.recognition_scope('b'*64,'old-batch'):
        for _ in range(2):provider_attempts.reserve(settings,'shazam','fingerprint','a'*64,'file_sha256')
    sends=[]
    async def sample(offset):return str(offset).encode(),offset
    async def identify(*args,**kwargs):
        try:provider_attempts.reserve(settings,'shazam','fingerprint','a'*64,'file_sha256')
        except provider_attempts.UsageError as exc:raise ProviderError(exc.code,'shazam','Synthetic spent budget')
        sends.append(1);return candidate('one')
    monkeypatch.setattr(recognition,'recognize',identify)
    result=asyncio.run(recognition.identify_segments(None,sample,120,lambda:None,
        input_revision='a'*64,audio_identity='b'*64,batch_id='new-batch'))
    assert len(sends)==1 and result['segments'][-1]['state']=='recognition_attempt_budget_exhausted'
    assert result['state']=='insufficient'
    assert provider_attempts.report(settings)['recognition_budget']['spent_calls']==3


def test_conflict_worker_catalog_and_owner_cas_decision_do_not_change_audio(settings,monkeypatch):
    cfg=replace(settings,external_fingerprints=True,read_only=False,storage_verified=True)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);tid,sha=seed(cfg)
        with db.transaction(cfg.db_path) as conn:
            meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()['metadata'])
            meta['duration']=120
            meta['audit']={'state':'passed','sha256':sha,'profile':'full_decode_all_audio_streams','audio_identity':'b'*64}
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),tid))
        monkeypatch.setattr(recognition,'sample',lambda path,roots,duration,offset=None:(str(offset).encode(),offset))
        async def identify(providers,audio,offset,*args,**kwargs):return candidate('one' if offset<40 else 'two')
        monkeypatch.setattr(recognition,'recognize',identify)
        queued=job(client,headers,'recognize',[tid]);drain(cfg)
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['record_type']=='possible_mix' and detail['possible_mix'] and not detail['can_write']
        assert detail['analysis']['state']=='needs_review' and detail['analysis']['recording_confirmation']=='conflict'
        assert len(detail['analysis']['recognition_segments'])==2
        assert 'candidate' not in detail['analysis']['recognition_segments'][0]
        denied=client.post('/api/jobs',json={'kind':'apply','track_ids':[tid]},headers={**headers,'Idempotency-Key':'mixed-apply'})
        assert denied.status_code==409 and denied.json()['code']=='possible_mix_requires_manual_review'
        route=f'/api/tracks/{tid}/recording-classification'
        body={'kind':'single_recording','base_version':0,'reason':'Owner checked the entire recording explicitly'}
        assert client.put(route,json=body,headers=headers).status_code==428
        assert client.put(route,json=body,headers={**headers,'If-Match':'"'+'f'*64+'"'}).status_code==412
        response=client.put(route,json=body,headers={**headers,'If-Match':f'"{sha}"'})
        assert response.status_code==200 and response.json()['version']==1
        assert client.put(route,json=body,headers={**headers,'If-Match':f'"{sha}"'}).status_code==412
        fresh=client.get('/api/tracks/'+tid).json()
        assert not fresh['possible_mix'] and fresh['can_write'] and fresh['record_type']=='single_recording'
        assert fresh['status']=='needs_review'  # classification is not tag acceptance
        assert media.sha256_file(cfg.work_root/'a.mp3')==sha
        history=db.fetchone(cfg.db_path,"SELECT * FROM history WHERE track_id=? AND kind='recording_classification'",(tid,))
        assert history['before_sha256']==history['after_sha256']==sha
        assert json.loads(history['details'])['audio_changed'] is False


def test_owner_classification_is_preserved_only_for_the_exact_audio_revision(settings):
    from mutagen.flac import FLAC
    from test_backend import synthetic
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);path=synthetic(settings)
        job(client,headers,'scan');drain(settings)
        old=client.get('/api/tracks').json()['items'][0]
        route=f'/api/tracks/{old["id"]}/recording-classification'
        response=client.put(route,json={'kind':'possible_mix','base_version':0,'reason':'Owner checked a compilation'},
                            headers={**headers,'If-Match':f'"{old["sha256"]}"'})
        assert response.status_code==200
        job(client,headers,'scan');drain(settings)
        assert client.get('/api/tracks/'+old['id']).json()['possible_mix']
        native=FLAC(path);native['title']=['Externally replaced metadata'];native.save()
        job(client,headers,'scan');drain(settings)
        current=client.get('/api/tracks').json()['items'][0]
        assert current['id']!=old['id'] and current['sha256']!=old['sha256']
        assert not current['possible_mix'] and current['record_type']=='audio'
        assert 'recording_classification' not in current
        assert current['input_readiness']['confirmations']==1
        prior=db.fetchone(settings.db_path,'SELECT metadata,is_current FROM tracks WHERE id=?',(old['id'],))
        assert not prior['is_current'] and json.loads(prior['metadata'])['recording_classification']['kind']=='possible_mix'


@pytest.mark.parametrize('status',['missing','error','conflict'])
def test_owner_record_type_annotation_preserves_critical_catalog_state(settings,status):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);tid,sha=seed(settings)
        with db.transaction(settings.db_path) as conn:
            conn.execute('UPDATE tracks SET status=? WHERE id=?',(status,tid))
        response=client.put(f'/api/tracks/{tid}/recording-classification',
            json={'kind':'possible_mix','base_version':0,'reason':'Owner metadata annotation without repairing input'},
            headers={**headers,'If-Match':f'"{sha}"'})
        assert response.status_code==200,response.text
        row=db.fetchone(settings.db_path,'SELECT status,sha256 FROM tracks WHERE id=?',(tid,))
        assert row['status']==status and row['sha256']==sha
        assert media.sha256_file(settings.work_root/'a.mp3')==sha


def mark_mix(client,headers,tid,sha):
    response=client.put(f'/api/tracks/{tid}/recording-classification',
        json={'kind':'possible_mix','base_version':0,'reason':'Owner found multiple recordings during background work'},
        headers={**headers,'If-Match':f'"{sha}"'})
    assert response.status_code==200,response.text


def test_long_audit_merges_fresh_owner_classification(settings,monkeypatch):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings);tid,sha=seed(settings)
        def audit(*args):
            mark_mix(client,headers,tid,sha)
            return {'state':'passed','sha256':sha,'profile':'fixture_classification_race_only'}
        monkeypatch.setattr(media,'audit_audio',audit)
        queued=job(client,headers,'audit',[tid]);drain(settings)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['possible_mix'] and detail['status']=='needs_review'
        assert detail['recording_classification']['origin']=='owner' and detail['recording_classification']['version']==1
        assert detail['audit']['state']=='passed' and detail['sha256']==sha


def derivative_source(cfg):
    tid,sha=seed(cfg)
    with db.transaction(cfg.db_path) as conn:
        meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()['metadata'])
        meta.update(format='ogg',write_supported=False)
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),tid))
    return tid,sha


def test_normalize_rechecks_classification_at_final_export_authorization(settings,monkeypatch):
    cfg=replace(settings,read_only=False,storage_verified=True)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);tid,sha=derivative_source(cfg)
        committed=[]
        def export(path,meta,settings,authorize):
            authorize()
            mark_mix(client,headers,tid,sha)
            authorize()  # same actual derivative callback before rename
            committed.append(True)
        monkeypatch.setattr('phonoteka.derivatives.export',export)
        queued=job(client,headers,'normalize_audio',[tid]);drain(cfg)
        assert not committed
        result=client.get('/api/jobs/'+queued['id']).json()
        assert result['state']!='succeeded'
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['possible_mix'] and detail['recording_classification']['version']==1
        assert 'audio_export' not in detail and media.sha256_file(cfg.work_root/'a.mp3')==sha


def test_completed_derivative_merge_preserves_late_owner_decision_and_child_guard(settings,monkeypatch):
    from test_backend import synthetic
    cfg=replace(settings,read_only=False,storage_verified=True)
    child=synthetic(cfg)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);tid,sha=derivative_source(cfg)
        def committed_export(path,meta,settings,authorize):
            authorize()
            # Simulate a decision arriving after an already-started successful
            # export commit, before its catalog bookkeeping. Codec validation
            # itself belongs to the existing real derivative tests.
            mark_mix(client,headers,tid,sha)
            return {'identity':'completed-normalize-fixture','files':[{'relative_path':child.name,'sha256':media.sha256_file(child)}]}
        monkeypatch.setattr('phonoteka.derivatives.export',committed_export)
        queued=job(client,headers,'normalize_audio',[tid]);drain(cfg)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['possible_mix'] and detail['recording_classification']['version']==1
        derived=client.get('/api/tracks/'+detail['audio_export']['track_ids'][0]).json()
        assert derived['possible_mix'] and not derived['can_write'] and derived['status']=='needs_review'
        assert 'recording_classification' not in derived
        assert media.sha256_file(cfg.work_root/'a.mp3')==sha


@pytest.mark.skipif(sys.platform!='linux',reason='Durable native write profile')
def test_begun_native_commit_keeps_late_mix_as_needs_review(settings,monkeypatch):
    from test_backend import synthetic
    cfg=replace(settings,read_only=False,storage_verified=True)
    path=synthetic(cfg)
    with TestClient(create_app(cfg),base_url=cfg.app_url) as client:
        headers=authenticated(client,cfg);job(client,headers,'scan');drain(cfg)
        track=client.get('/api/tracks').json()['items'][0];tid,sha=track['id'],track['sha256']
        response=client.post(f'/api/tracks/{tid}/proposal',json={'tags':{'title':'Native committed update'}},headers={**headers,'If-Match':f'"{sha}"'})
        assert response.status_code==201,response.text
        original=media.apply_changes
        def apply_then_owner(*args,**kwargs):
            result=original(*args,**kwargs)
            mark_mix(client,headers,tid,sha)
            return result
        monkeypatch.setattr(media,'apply_changes',apply_then_owner)
        queued=job(client,headers,'apply',[tid]);drain(cfg)
        assert client.get('/api/jobs/'+queued['id']).json()['state']=='succeeded'
        detail=client.get('/api/tracks/'+tid).json()
        assert detail['tags']['title']=='Native committed update' and detail['sha256']!=sha
        assert detail['possible_mix'] and detail['status']=='needs_review' and not detail['can_write']
        assert detail['recording_classification']['input_sha256']==detail['sha256']
        assert media.sha256_file(cfg.source_root/path.name)==sha
