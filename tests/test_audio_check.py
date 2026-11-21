from dataclasses import replace
import json
import uuid

import pytest

from phonoteka import audio_check, db, media, preparation, provider_policy
from phonoteka.worker import root_identity
from test_backend import settings as base_settings
from test_confirmed_fields import library
from test_provider_policy import grant, lyrics


@pytest.fixture
def settings(tmp_path):
    return replace(base_settings.__wrapped__(tmp_path), inference_url='http://test-inference')


@pytest.fixture
def report(library):
    cfg, client, headers, tid, sha, path = library
    scan, identity = str(uuid.uuid4()), str(uuid.uuid4())
    with db.transaction(cfg.db_path) as conn:
        grant(conn)
        candidate = {**lyrics(), 'instrumental_claim':False, 'lrc':'[00:01]Тихая река',
                     'match':{'state':'matching_metadata'}}
        analysis = provider_policy.prepare_analysis(conn, {'candidates':[candidate]})
        conn.execute('UPDATE analyses SET payload=?,input_sha256=? WHERE track_id=?',
                     (db.encode(analysis),sha,tid))
        run = {'id':identity, 'report_id':identity, 'scan_id':scan, 'refines':str(uuid.uuid4()),
               'root':root_identity(cfg.work_root), 'epoch':int(db.setting(cfg.db_path,'auth_epoch')), 'state':'review'}
        preparation.save(conn, run)
        conn.execute('INSERT INTO settings VALUES(?,?)', ('fill_page:'+scan+':0', db.encode([[tid,sha]])))
        conn.execute('INSERT INTO settings VALUES(?,?)', ('fill_report:'+identity+':0',
                     db.encode([[tid,path.name,'found','lyrics_option_found']])))
    return cfg,client,headers,tid,sha,path,'/api/preparation/'+identity+'/audio-check'


def test_real_report_queues_only_missing_words_once_without_media_writes(report):
    cfg,client,headers,tid,sha,path,url=report
    response=client.get(url)
    assert response.status_code==200, response.text
    row,=response.json()['items']
    assert row['id']==tid and row['state']=='ready' and row['language']=='ru' and row['variants']==1
    assert client.get(url+'?offset=60').json()['items']==[]
    body={'tokens':{tid:row['token']}}
    assert client.post(url, json=body).status_code!=202
    assert client.post(url, json={'tokens':{tid:'stale'}}, headers=headers).status_code==409
    first=client.post(url, json=body, headers=headers)
    assert first.status_code==202, first.text
    job=first.json()['job_id']
    assert client.post(url, json=body, headers=headers).json()['job_id']==job
    assert client.get(url).json()['items'][0]['state']=='busy'
    payload=db.fetchone(cfg.db_path,'SELECT kind,payload FROM jobs WHERE id=?',(job,))
    item,=json.loads(payload['payload'])['items']
    assert payload['kind']=='transcribe' and item['language_hint']=='ru' and item['sha256']==sha
    assert 'reference_text' not in item and media.sha256_file(path)==sha
    with db.transaction(cfg.db_path) as conn:
        track=conn.execute('SELECT * FROM tracks WHERE id=?',(tid,)).fetchone()
        audio_check.recheck(conn,track,item)
        grant(conn,allow_derived_analysis=False)
        with pytest.raises(media.MediaError,match='.*'):
            audio_check.recheck(conn,track,item)
    assert client.get(url).json()['items'][0]['state']=='no_match'


@pytest.mark.parametrize('change,state',[('stale','changed'),('instrumental','blocked'),('speech','blocked'),('usage','no_match')])
def test_changed_or_excluded_items_cannot_spend_asr(report, change, state):
    cfg,client,headers,tid,sha,path,url=report
    row,=client.get(url).json()['items']
    with db.transaction(cfg.db_path) as conn:
        if change=='usage':
            grant(conn,allow_derived_analysis=False)
        elif change=='stale':
            conn.execute('UPDATE tracks SET sha256=? WHERE id=?',('a'*64,tid))
        elif change=='speech':
            conn.execute("UPDATE tracks SET metadata=json_set(metadata,'$.tags.genre',json_array('Podcast')) WHERE id=?",(tid,))
        else:
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('lyrics_review:'+tid,db.encode({'instrumental_confirmed':True})))
    assert client.get(url).json()['items'][0]['state']==state
    assert client.post(url, json={'tokens':{tid:row['token']}}, headers=headers).status_code==409
    assert media.sha256_file(path)==sha


def test_gigaam_word_anchors_use_real_unscored_words_and_exclude_blank_lines():
    words=[{'word':'тихая','start':1.2,'end':1.4},{'word':'река','start':1.4,'end':1.8}]
    asr={'engine':'gigaam','word_probabilities':False,'words':words}
    text='[00:01.00]Тихая река\n[00:02.00]\n[00:03.00]Рассвет настал'
    result=audio_check.compare([text],asr,4)
    assert result=={'state':'partial','anchored':1,'lines':2,'median_ms':200}
    assert audio_check.compare([text],{**asr,'engine':'whisper'},4)['anchored']==0
    assert audio_check.compare(['[00:01.00]Тихая река'],asr,4)['state']=='matched'


def test_existing_results_require_a_matching_job_receipt_and_russian_engine(report):
    cfg,client,headers,tid,sha,path,url=report
    current=db.fetchone(cfg.db_path,'SELECT payload FROM analyses WHERE track_id=?',(tid,))
    data=json.loads(current['payload'])
    asr={'input_sha256':sha,'remote_job_id':'test-engine-result','engine':'gigaam','language':'ru',
         'word_probabilities':False,'words':[{'word':'тихая','start':0.1,'end':0.2},
                                            {'word':'река','start':0.2,'end':0.3}]}
    with db.transaction(cfg.db_path) as conn:
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?',(db.encode({**data,'asr':asr}),tid))
    assert client.get(url).json()['items'][0]['state']=='ready'
    local=str(uuid.uuid4())
    now=db.utcnow()
    with db.transaction(cfg.db_path) as conn:
        conn.execute("""INSERT INTO jobs(id,kind,state,stage,payload,total,created_at,updated_at)
            VALUES(?,'transcribe','succeeded','completed',?,1,?,?)""",
            (local,db.encode({'items':[{'track_id':tid,'sha256':sha}]}),now,now))
        conn.execute('INSERT INTO job_items VALUES(?,?,?,?)',
                     (local,tid,'succeeded',db.encode({'inference_job_id':'test-engine-result'})))
    row,=client.get(url).json()['items']
    assert row['state']=='matched' and row['anchored']==row['lines']==1 and 'token' not in row
    with db.transaction(cfg.db_path) as conn:
        conn.execute('UPDATE analyses SET payload=? WHERE track_id=?',
                     (db.encode({**data,'asr':{**asr,'engine':'whisper'}}),tid))
    assert client.get(url).json()['items'][0]['state']=='ready'
