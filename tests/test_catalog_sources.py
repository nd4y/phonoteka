"""Completed recovery/CUE outputs must not leave their inputs in the work queue."""
from dataclasses import replace
import json
import time

import pytest
from fastapi.testclient import TestClient

from phonoteka import catalog_sources, db, preparation, provider_policy
from phonoteka.app import create_app
from test_backend import settings, client, authenticated, seed, job


def prepared(settings, cue=False, legacy=False):
    source,sha=seed(settings,'source.mp3')
    parent,parent_sha=seed(settings,'album.cue') if cue else (source,sha)
    children=[seed(settings,f'prepared/{index}.flac') for index in range(2 if cue else 1)]
    with db.transaction(settings.db_path) as conn:
        def update(tid,changes,status='needs_review'):
            meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
            meta.update(changes)
            conn.execute('UPDATE tracks SET metadata=?,status=? WHERE id=?',(db.encode(meta),status,tid))
        export={'identity':'export-fixture','track_ids':[tid for tid,_ in children],'count':len(children)}
        if cue:
            update(parent,{'format':'cue','cue_export':export})
            update(source,{'cue_album_image':True,'cue_sheets':[{'id':parent,'track_count':2}]})
        else:
            update(source,{'audio_export':export,'input_error':{'code':'decode_failed'},
                           'audit':{'sha256':sha,'state':'failed'}},'error')
        for tid,revision in children:
            origin={'parent_track_id':parent,'parent_sha256':parent_sha,
                    'export_identity':export['identity'],'source':'source.mp3','source_sha256':sha}
            if cue:origin.update(sheet_id=parent,sheet_sha256=parent_sha)
            if legacy:
                origin.pop('parent_track_id');origin.pop('parent_sha256')
            update(tid,{'format':'flac','tags':{'title':['Song'],'artist':['Artist'],'album':['Album']},
                        'has_art':True,'lrc':'[00:01.00]Line','audit':{'sha256':revision,'state':'passed'},
                        'derived_from':origin})
    return source,parent,[tid for tid,_ in children]


def test_recovered_input_is_preserved_but_not_an_outstanding_deliverable(client,settings):
    authenticated(client,settings)
    source,_,children=prepared(settings)
    counts=client.get('/api/reports/collection').json()['counts']
    assert counts['files']==2 and counts['audio_files']==counts['source_audio_files']==1
    assert counts['with_core_tags']==counts['with_cover']==counts['with_lyrics']==1
    assert counts['audit_errors']==counts['input_errors']==0 and counts['audit_clean']==1
    assert client.get('/api/tracks?filter=all').json()['total']==2
    for name in ('missing_art','missing_lyrics','audit_errors'):
        assert client.get('/api/tracks',params={'filter':name}).json()['total']==0
    assert [row['id'] for row in client.get('/api/tracks?filter=review').json()['items']]==children
    assert client.get('/api/tracks/'+source).status_code==200
    exported={r['id']:r for r in client.get('/api/reports/export/json').json()['files']}
    assert exported[source]['collection_role']=='source'
    assert json.loads(exported[source]['prepared_track_ids'])==children
    assert exported[children[0]]['collection_role']=='track'
    assert not exported[children[0]]['published_current_revision']


@pytest.mark.parametrize('broken',['missing','superseded','parent_revision','export_identity','different_parent'])
def test_unproven_outputs_do_not_hide_unfinished_input(client,settings,broken):
    authenticated(client,settings)
    source,_,children=prepared(settings)
    with db.transaction(settings.db_path) as conn:
        if broken=='missing':conn.execute("UPDATE tracks SET status='missing' WHERE id=?",(children[0],))
        elif broken=='superseded':conn.execute('UPDATE tracks SET is_current=0 WHERE id=?',(children[0],))
        else:
            meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(children[0],)).fetchone()[0])
            key={'parent_revision':'parent_sha256','export_identity':'export_identity','different_parent':'parent_track_id'}[broken]
            meta['derived_from'][key]='different'
            conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),children[0]))
        assert source not in catalog_sources.completed_sources(conn)
    assert client.get('/api/reports/collection').json()['counts']['source_audio_files']==0
    assert source in [r['id'] for r in client.get('/api/tracks?filter=audit_errors').json()['items']]


@pytest.mark.parametrize('legacy',[False,True])
def test_cue_image_is_excluded_only_after_all_children_match_current_source(client,settings,legacy):
    authenticated(client,settings)
    source,sheet,children=prepared(settings,cue=True,legacy=legacy)
    counts=client.get('/api/reports/collection').json()['counts']
    assert counts['files']==4 and counts['cue_sheets']==counts['source_audio_files']==1 and counts['audio_files']==2
    assert counts['with_cover']==counts['with_lyrics']==2
    with db.transaction(settings.db_path) as conn:
        assert set(catalog_sources.completed_sources(conn))=={source,sheet}
        conn.execute('UPDATE tracks SET sha256=? WHERE id=?',('0'*64,source))
    assert client.get('/api/reports/collection').json()['counts']['source_audio_files']==0
    with db.transaction(settings.db_path) as conn:
        conn.execute("UPDATE tracks SET status='missing' WHERE id=?",(children[0],))
        assert catalog_sources.completed_sources(conn)=={}


def test_legacy_cue_fields_do_not_override_conflicting_parent_proof(client,settings):
    authenticated(client,settings)
    source,sheet,children=prepared(settings,cue=True)
    with db.transaction(settings.db_path) as conn:
        meta=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(children[0],)).fetchone()[0])
        meta['derived_from']['parent_sha256']='0'*64
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),children[0]))
        assert catalog_sources.completed_sources(conn)=={}


def test_bulk_and_folder_preparation_use_outputs_without_requeuing_inputs(settings):
    settings=replace(settings,external_metadata=True,external_fingerprints=True)
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings)
        source,_,children=prepared(settings)
        with db.transaction(settings.db_path) as conn:
            conn.execute("UPDATE tracks SET status='indexed' WHERE id=?",(source,))
        scan=job(client,headers,'scan')['id']
        with db.transaction(settings.db_path) as conn:
            for row in conn.execute('SELECT * FROM tracks').fetchall():
                conn.execute('INSERT INTO job_items VALUES(?,?,?,?)',(scan,row['relative_path'],'succeeded',
                    db.encode({'track_id':row['id'],'sha256':row['sha256']})))
            for kind in ('recognize','analyze'):
                assert [r['track_id'] for r in preparation.pending(conn,scan,kind,'')]==children
        for kind in ('recognize','analyze'):
            queued=job(client,headers,kind,params={'scope':'all'})
            payload=json.loads(db.fetchone(settings.db_path,'SELECT payload FROM jobs WHERE id=?',(queued['id'],))['payload'])
            assert [item['track_id'] for item in payload['items']]==children


@pytest.mark.parametrize('provider,data_type,kind,counter,match',[
    ('shazam','metadata','recognize','recognition_pending','fingerprint_match'),
    ('lrclib','lyrics','analyze','metadata_pending','candidates'),
])
def test_pending_report_matches_launch_scope_for_expired_results(client,settings,provider,data_type,kind,counter,match):
    headers=authenticated(client,settings)
    tid,sha=seed(settings,'file.mp3')
    scan=job(client,headers,'scan')['id']
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO job_items VALUES(?,?,?,?)',(scan,'file.mp3','succeeded',
            db.encode({'track_id':tid,'sha256':sha})))
        policy=provider_policy.defaults(provider,data_type)
        policy.update(allow_cache=True,cache_ttl=60)
        key=f'provider_usage:{provider}:{data_type}'
        conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,db.encode(policy)))
    for age,cache_allowed,expected in [(0,True,0),(120,True,1),(120,False,0)]:
        with db.transaction(settings.db_path) as conn:
            policy['allow_cache']=cache_allowed
            conn.execute('UPDATE settings SET value=? WHERE key=?',(db.encode(policy),key))
            usage={**policy,'origin':'provider_acquired','acquired_at':time.time()-age}
            sources=[{'provider':p,'state':match if p==provider else 'no_match'} for p in ('musicbrainz','lrclib','shazam')]
            candidate={'id':'fixture:1','provider':provider,'kind':'lyrics' if data_type=='lyrics' else 'metadata','usage':usage}
            candidate.update({'lrc':'[00:01.00]Line'} if data_type=='lyrics' else {'tags':{'title':'Song'}})
            payload={'sources':sources,'candidates':[candidate]}
            conn.execute('INSERT OR REPLACE INTO analyses VALUES(?,?,?,?)',(tid,sha,db.encode(payload),db.utcnow()))
            scope=preparation.pending(conn,scan,kind,'')
        counts=client.get('/api/reports/collection').json()['counts']
        assert counts[counter]==len(scope)==expected
