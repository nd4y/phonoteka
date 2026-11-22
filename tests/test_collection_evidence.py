import json

from phonoteka import collection_evidence as evidence, db, edition_artwork, enrichment_gaps, media
from phonoteka.timing_comparison import summarize
from phonoteka.timing_comparison import recover_unique_lines
from test_audio_check import report, settings
from test_confirmed_fields import library

RID='a41c0976-8770-488b-9f70-869949fcade6'
ED='46e015b7-53bd-4521-bade-b765a0f9fc12'


def test_signed_line_diagnostics_keep_missing_and_silent_rows_distinct():
    lines=[{'text':'intro','state':'anchored','original_ms':0,'start_ms':1400},
           {'text':'','state':'unaligned','original_ms':2000,'start_ms':None},
           {'text':'middle','state':'anchored','original_ms':4000,'start_ms':5300},
           {'text':'end','state':'anchored','original_ms':11000,'start_ms':12400}]
    result=summarize(lines)
    assert result['classification']=='constant_offset' and result['offset_ms']==1400
    assert [r['line'] for r in result['line_details']]==[1,3,4]
    assert result['line_details'][0]['reference_ms']==0 and 'intro' not in json.dumps(result)
    lines[-1].update(state='unaligned',start_ms=None)
    result=summarize(lines)
    assert result['classification']=='partial' and result['line_details'][-1]['delta_ms'] is None


def test_instrumental_needs_the_exact_performance_without_vocal_or_other_unmarked_works():
    meta={'tags':{'title':'Piece','artist':'Band'},'duration':100}
    rel={'type':'performance','target-type':'work','work':{'id':RID},'attributes':['instrumental']}
    doc={'id':RID,'artist-credit':[{'name':'Band'}],'title':'Piece','length':100000,'relations':[rel]}
    assert evidence.performance(doc,RID,meta)=='instrumental_confirmed'
    assert evidence.performance({**doc,'relations':[rel,{**rel,'attributes':[]}]},RID,meta)=='no_performance_evidence'
    assert evidence.performance({**doc,'relations':[rel,{'type':'vocal','target-type':'artist'}]},RID,meta)=='vocal_credit'
    assert evidence.performance({**doc,'title':'Another piece'},RID,meta)=='identity_conflict'
    assert not enrichment_gaps.verified_instrumental({'sha256':'new','instrumental_evidence':{'profile':evidence.PROFILE,'file_sha256':'old'}})


def test_line_recovery_ignores_reference_time_and_holds_repeated_occurrences():
    line={'text':'one two three','state':'unaligned','original_ms':12000,'start_ms':None}
    words=[{'word':s,'start':i,'end':i+.3,'probability':1} for i,s in enumerate(['one','two','three'])]
    found,=recover_unique_lines([line],words,20,False)
    assert found['recovered'] and found['start_ms']==0 and found['original_ms']==12000
    assert recover_unique_lines([line,line],words,20,False)==[line,line]
    repeated=words+[{**w,'start':w['start']+5,'end':w['end']+5} for w in words]
    assert recover_unique_lines([line],repeated,20,False)==[line]


def test_edition_requires_exhaustive_catalog_and_exact_release_slot():
    entry={'id':ED,'title':'One','status':'Official'}
    page={'releases':[entry],'release-count':1,'release-offset':0}
    assert edition_artwork.unique_edition(page,'One','')==ED
    assert edition_artwork.unique_edition({**page,'release-count':2},'One','') is None
    assert edition_artwork.unique_edition({**page,'releases':[entry,{**entry,'id':RID}],'release-count':2},'One','') is None
    ref={'title':'One','status':'Official','tracks':[{'slot':'1:2','disc':1,'position':2,'recording_id':RID,
          'release_track_id':ED,'artist':'Band','title':'Song','duration':100}]}
    meta={'tags':{'album':'One','artist':'Band','title':'Song','tracknumber':'2/10'},'duration':100}
    assert edition_artwork.release_slot(ref,RID,meta)=='1:2'
    meta['tags']['tracknumber']='3/10'
    assert edition_artwork.release_slot(ref,RID,meta) is None


def test_report_requires_current_manifest_csrf_and_idempotently_starts_real_queue(report):
    cfg,client,headers,tid,sha,path,base=report
    url=base.removesuffix('audio-check')+'evidence'
    assert client.get(url).json()['state']=='idle'
    assert client.post(url,json={}).status_code!=202
    with db.transaction(cfg.db_path) as conn:
        from phonoteka import preparation
        run=preparation.load(conn)
        run['autofill']=True
        preparation.save(conn,run)
    started=client.post(url,headers=headers)
    assert started.status_code==202,started.text
    job=started.json()['job_id']
    assert client.post(url,headers=headers).json()['job_id']==job
    frozen=db.fetchone(cfg.db_path,'SELECT kind,payload FROM jobs WHERE id=?',(job,))
    item,=json.loads(frozen['payload'])['items']
    assert frozen['kind']==evidence.KIND and item['sha256']==sha
    assert client.get(url).json()['total']==1 and media.sha256_file(path)==sha
    assert client.get(url+'/covers').status_code==409
    from test_backend import drain
    drain(cfg)
    checked=client.get(url+'?group=cover').json()
    assert checked['state']=='succeeded' and checked['completed']==1 and checked['failed']==0
    assert checked['counts']=={'cover':{'edition_unconfirmed':1}}
    assert client.get(url+'/covers').json()=={'tokens':{}}
    assert media.sha256_file(path)==sha
    assert client.post(url+'/retry?expected_round=0',headers=headers).status_code==409
    with db.transaction(cfg.db_path) as conn:
        conn.execute('UPDATE job_items SET result=? WHERE job_id=? AND item_key=?',
                     (db.encode({'group':'cover','outcome':'source_unavailable','code':'provider_cooldown','revision':sha}),job,tid))
    assert client.get(url).json()['retryable']==1
    assert client.post(url+'/retry?expected_round=0',headers=headers).status_code==202
    assert client.post(url+'/retry?expected_round=0',headers=headers).json()['round']==1
    assert client.get(url).json()['completed']==0
    drain(cfg)
    assert client.get(url).json()['counts']=={'cover':{'edition_unconfirmed':1}}
    assert client.get(url).json()['completed']==1 and media.sha256_file(path)==sha


def test_renewal_requires_an_existing_reference_and_original_permission():
    usage={'provider':'lrclib','allow_derived_analysis':True,'allow_cache':True,'cache_ttl':60}
    c={'provider':'lrclib','id':'lrclib:123','usage':usage,'transient_only':True,
       'match':{'state':'matching_metadata'},'instrumental_claim':False}
    a={'asr':{'words':[{}]},'alignment_usage':[{}],'candidates':[c]}
    assert evidence.expired_timing(a)==[123]
    assert not evidence.expired_timing({**a,'asr':{}})
    assert not evidence.expired_timing({**a,'alignment_usage':[]})
    assert not evidence.expired_timing({**a,'candidates':[{**c,'usage':{**usage,'allow_cache':False}}]})
    assert not evidence.expired_timing({**a,'candidates':[{**c,'instrumental_claim':True}]})


def test_redacted_fingerprint_can_neither_identify_nor_contradict_on_missing_tags(monkeypatch):
    monkeypatch.setattr(evidence.fields,'recording',lambda *args:([],[],'recording_unconfirmed'))
    meta={'duration':120,'tags':{'title':'Song','artist':'Artist','musicbrainz_trackid':ED}}
    c={'identification':'audio_fingerprint','provider':'acoustid','recording_id':ED,
       'match':{'state':'fingerprint_match'},'transient_only':True}
    assert evidence.bound_recording(None,meta,{'candidates':[c]})==ED
    assert evidence.bound_recording(None,{'duration':120,'tags':{'title':'Song','artist':'Artist'}},{'candidates':[c]}) is None
    assert evidence.bound_recording(None,meta,{'candidates':[{**c,'recording_id':'9eb20c16-5678-4770-988b-9f70e69dba13'}]}) is None
    assert evidence.bound_recording(None,meta,{'candidates':[{**c,'tags':{'artist':'Different','title':'Song'}}]}) is None
