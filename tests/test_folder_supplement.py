"""Exercise the continuation on the original scan, including candidates from older searches."""
import json

import pytest
from phonoteka import artwork_preparation, collection_fill, db, media, preparation
from test_audio_check import report, settings
from test_confirmed_fields import RID, R1, R2, library, plan, update_analysis


def supplement(report, *, changed=False):
    cfg, client, headers, tid, sha, path, url = report
    with db.transaction(cfg.db_path) as conn:
        run = preparation.load(conn)
        run.update(autofill=True, scanned_files=1, scan_errors=0, profile='lyrics_required', folder='')
        preparation.save(conn, run)
        conn.execute('DELETE FROM settings WHERE key GLOB ?', ('fill_report:' + run['report_id'] + ':*',))
        if changed:
            conn.execute('UPDATE settings SET value=? WHERE key=?',
                         (db.encode([[tid,'0'*64]]), 'fill_page:'+run['scan_id']+':0'))
    endpoint = url.rsplit('/',1)[0]+'/supplement'
    assert client.post(endpoint).status_code != 202
    result = client.post(endpoint, headers=headers)
    assert result.status_code == 202, result.text
    assert client.post(endpoint, headers=headers).json()['id'] == result.json()['id']
    with db.transaction(cfg.db_path) as conn:
        run=preparation.load(conn)
        collection_fill.advance(conn,cfg,run)
        preparation.save(conn,run)
    return run


@pytest.mark.parametrize('changed',[False,True])
def test_older_lyrics_checked_only_for_scanned_revision(report,changed):
    cfg, client, _, tid, sha, path, _ = report
    run = supplement(report,changed=changed)
    if changed:
        assert not run.get('job_id') and run['reasons']['file_revision_changed']==1
    else:
        job=db.fetchone(cfg.db_path,'SELECT * FROM jobs WHERE id=?',(run['job_id'],))
        assert job['kind']=='transcribe'
        item,=json.loads(job['payload'])['items']
        assert item['track_id']==tid and item['sha256']==sha and item['language_hint']=='ru'
        assert item['audio_check_token'] and 'reference_text' not in item
    assert media.sha256_file(path)==sha


def test_tagged_album_is_a_distinct_lookup_not_an_edition_decision():
    tags={'title':'Song','artist':'Artist','album':'Album'}
    analysis={'query':tags,'sources':[{'provider':'musicbrainz','state':'no_match'}]}
    assert artwork_preparation.named_album_lookup({'tags':tags},analysis)=='Album'
    for flag in ('multiple_recordings','possible_mix','critical_conflicts'):
        assert artwork_preparation.named_album_lookup({'tags':tags},{**analysis,flag:True}) is None
    assert not artwork_preparation.named_album_lookup({'tags':tags},{**analysis,'query_source':'album_hint'})
    assert not artwork_preparation.named_album_lookup({'tags':tags},{**analysis,'sources':analysis['sources']+[{'provider':'coverartarchive','state':'unknown_outcome'}]})


@pytest.mark.parametrize('edition,conflict,ready',[(R1,False,True),('',False,False),([R1,R2],False,False),(R1,True,False)])
def test_embedded_reference_can_only_fill_art_for_the_pinned_release(library,edition,conflict,ready):
    cfg,client,_,tid,_,_=library
    with db.transaction(cfg.db_path) as conn:
        m=json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?',(tid,)).fetchone()[0])
        m['duration']=10
        m['tags'].update(title='Original',artist='Artist',album='Confirmed album',musicbrainz_trackid=RID,musicbrainz_albumid=edition)
        conn.execute('UPDATE tracks SET metadata=? WHERE id=?',(db.encode(m),tid))
    def edit(a):
        c=a['candidates']
        if conflict:
            c[0]['recording_id']=R2
        else:
            c.pop(0)
        next(i for i in c if i['provider']=='musicbrainz' and i['kind']=='metadata')['duration']=10
    update_analysis(cfg,tid,edit)
    entry=plan(client)
    assert entry['ready'] is ready
    assert [v['field'] for v in entry['changes']]==(['cover'] if ready else [])


@pytest.mark.parametrize('ambiguous', [False, True])
def test_plain_reference_requires_one_exact_permitted_text(report,ambiguous):
    cfg,_,_,tid,sha,path,_=report
    def edit(a):
        base=a['candidates'][0]
        base.update(lrc='',plain_lyrics='Тихая река')
        if ambiguous:
            a['candidates'].append({**base,'plain_lyrics':'Другие слова'})
    update_analysis(cfg,tid,edit)
    run=supplement(report)
    if ambiguous:
        assert not run.get('job_id')
    else:
        job=db.fetchone(cfg.db_path,'SELECT * FROM jobs WHERE id=?',(run['job_id'],))
        item,=json.loads(job['payload'])['items']
        assert job['kind']=='transcribe' and item['audio_check_plain'] and item['language_hint']=='ru'
        assert 'reference_text' not in item
    assert media.sha256_file(path)==sha
