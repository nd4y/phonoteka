import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest

from phonoteka import db, exports
from test_backend import settings, client, authenticated
from test_exports import published


def test_latest_preserves_full_manifest_and_receipts_and_closes_file(client, settings, monkeypatch):
    headers=authenticated(client,settings)
    track_id,sha,_=published(settings)
    row=db.fetchone(settings.db_path,'SELECT metadata FROM tracks WHERE id=?',(track_id,))
    meta=json.loads(row['metadata']);meta['youtube']={'evidence_note':'Unicode evidence: я 🎵'}
    with db.transaction(settings.db_path) as conn:
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),track_id))
    identity=exports.generate(settings)['export_id']
    expected=exports.manifest(settings,identity)
    saved=client.post('/api/deliveries',headers=headers,json={
        'export_id':identity,'device':'fixture','checksums':{expected['files'][0]['path']:sha},
    })
    assert saved.status_code==201
    streams=[];original=exports._latest_response

    def capture(value):
        response=original(value);streams.append(response.report_stream)
        return response

    monkeypatch.setattr(exports,'_latest_response',capture)
    response=client.get('/api/exports/latest')
    assert response.status_code==200
    document=response.json()
    assert document['export']==expected
    assert document['ready_count']==1 and document['stale_files']==0
    assert len(document['receipts'])==1
    assert document['receipts'][0]['id']==saved.json()['id']
    assert document['receipts'][0]['state']=='client_hashes_match'
    assert document['receipts'][0]['poweramp_checked'] is False
    assert len(streams)==1 and streams[0].closed
    assert response.headers['content-type']=='application/json'
    assert response.headers['cache-control']=='private, no-store'


def test_latest_file_closes_if_serialization_fails(monkeypatch):
    streams=[];original=exports.tempfile.TemporaryFile

    def capture(*args,**kwargs):
        stream=original(*args,**kwargs);streams.append(stream)
        return stream

    monkeypatch.setattr(exports.tempfile,'TemporaryFile',capture)
    with pytest.raises(ValueError):
        exports._latest_response({'partial':'written before failure','invalid':float('nan')})
    assert len(streams)==1 and streams[0].closed


def test_audio_cache_rechecks_policy_and_audio_hash(client, settings):
    from phonoteka import provider_policy
    from test_provider_policy import grant, lyrics
    authenticated(client,settings)
    track_id,_,_=published(settings)
    with db.transaction(settings.db_path) as conn:
        grant(conn)
        row=conn.execute('SELECT metadata FROM tracks WHERE id=?',(track_id,)).fetchone()
        meta=json.loads(row['metadata'])
        meta['usage_provenance']={'lrc':{'usage':provider_policy.candidate_usage(conn,lyrics())}}
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(meta),track_id))
    identity=exports.generate(settings)['export_id']
    entry=exports.manifest(settings,identity)['files'][0]
    route=f'/api/exports/{identity}/files/'+quote(entry['path'],safe='/')
    assert client.get(route).status_code==200
    with db.transaction(settings.db_path) as conn:grant(conn,allow_export=False)
    denied=client.get(route)
    assert denied.status_code==409 and denied.json()['code']=='provider_policy_denied'
    with db.transaction(settings.db_path) as conn:grant(conn)
    path=settings.state_dir/'exports'/identity/'files'/entry['path']
    path.write_bytes(b'changed audio after index was cached')
    assert client.get(route).status_code==409


def test_audio_cache_invalidates_same_size_manifest_with_restored_mtime(client, settings):
    authenticated(client,settings)
    _,sha,_=published(settings)
    identity=exports.generate(settings)['export_id']
    entry=exports.manifest(settings,identity)['files'][0]
    route=f'/api/exports/{identity}/files/'+quote(entry['path'],safe='/')
    assert client.get(route).status_code==200
    path=settings.state_dir/'exports'/identity/'manifest.json'
    stamp=path.stat();original=path.read_bytes()
    changed=original.replace(sha.encode(),b'0'*64)
    assert len(changed)==len(original) and changed!=original
    path.write_bytes(changed);os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert client.get(route).status_code==409
    path.unlink()
    assert client.get(route).status_code==404


def test_concurrent_audio_lookups_parse_one_manifest_and_release_display_payload(settings, monkeypatch):
    db.initialize(settings.db_path)
    published(settings)
    identity=exports.generate(settings)['export_id']
    document=exports.manifest(settings,identity);entry=document['files'][0]
    index=exports.FileIndex();reads=[];barrier=threading.Barrier(8)
    original=exports._read_manifest

    def read(*args):
        reads.append(1);time.sleep(.03)
        return original(*args)

    def lookup(_):
        barrier.wait(timeout=5)
        return index.get(settings,identity,entry['path'])

    monkeypatch.setattr(exports,'_read_manifest',read)
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lookup,range(8)))
    assert len(reads)==1
    assert all(result==(identity,True,{'sha256':entry['sha256'],'usage_provenance':entry['usage_provenance']}) for result in results)
