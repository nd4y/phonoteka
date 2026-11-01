import json
from phonoteka import db
from test_backend import settings, client, authenticated, seed


def test_overview_denominators_current_revisions_and_audit_errors(client, settings):
    authenticated(client, settings)
    records = [seed(settings, relative=f'file-{index}.mp3') for index in range(6)]
    with db.transaction(settings.db_path) as conn:
        for index, (tid, sha) in enumerate(records):
            meta = json.loads(conn.execute('SELECT metadata FROM tracks WHERE id=?', (tid,)).fetchone()[0])
            status = 'scanned'
            if index == 0:
                meta.update(tags={'title':['Song'], 'artist':['Artist'], 'album':['Album']}, has_art=True,
                            lrc='[00:01.00]A line', audit={'sha256':sha,'state':'passed','warnings':[]}, publication={'sha256':sha})
            elif index == 1:
                meta.update(input_error={'code':'invalid_header'}, audit={'sha256':sha,'state':'passed','warnings':[]})
                status = 'error'
            elif index == 2:
                meta.update(audit={'sha256':'0'*64,'state':'passed','warnings':[]}, publication={'sha256':'0'*64})
            elif index == 3:
                meta['audit']={'sha256':sha,'state':'passed','warnings':[{'code':'high_sample_peak'}]}
            elif index == 4:
                meta.update(format='cue', tags={'title':['Document'],'artist':['Artist'],'album':['Album']}, has_art=True,lrc='[00:01.00]Not audio')
            else:
                meta['audit']={'sha256':sha,'state':'failed','error':'decode_failed','failure_stage':'decode'}
                status = 'error'
            conn.execute('UPDATE tracks SET metadata=?,status=? WHERE id=?', (db.encode(meta),status,tid))
        for index, revision, sources in (
            (0, records[0][1], [{'provider':'musicbrainz','state':'candidates'},{'provider':'lrclib','state':'no_match'},{'provider':'shazam','state':'fingerprint_match'}]),
            (2, '0'*64, [{'provider':'shazam','state':'fingerprint_match'}]),
            (3, records[3][1], [{'provider':'musicbrainz','state':'no_match'},{'provider':'lrclib','state':'budget_exhausted'},{'provider':'shazam','state':'no_match'}]),
        ):
            conn.execute('INSERT INTO analyses VALUES(?,?,?,?)', (records[index][0],revision,db.encode({'sources':sources,'candidates':[]}),db.utcnow()))
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('current',records[0][0],records[0][1],'{}','2026-01-01'))
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('older-current',records[2][0],records[2][1],'{}','2026-01-01'))
        conn.execute('INSERT INTO proposals VALUES(?,?,?,?,?)', ('newer-stale',records[2][0],'0'*64,'{}','2026-01-02'))
    report = client.get('/api/reports/collection').json()
    counts = report['counts']
    assert counts['files'] == 6 and counts['audio_files'] == 5 and counts['cue_sheets'] == 1
    assert counts['with_core_tags'] == counts['with_cover'] == counts['with_lyrics'] == 1
    assert counts['input_errors'] == 1 and counts['audit_errors'] == 2
    assert counts['audit_clean'] == counts['audit_warnings'] == counts['audit_unchecked'] == 1
    assert sum(counts[key] for key in ('audit_clean','audit_warnings','audit_unchecked','audit_errors')) == counts['audio_files']
    assert counts['current_proposals'] == counts['published_current'] == 1
    assert counts['analyzed'] == 2 and counts['stale_analysis'] == 1
    assert counts['eligible_files'] == 3 and counts['metadata_pending'] == 2 and counts['recognition_pending'] == 1
    assert counts['fingerprint_matches'] == 1  # The old revision is not counted.
    assert client.get('/api/tracks?filter=audit_errors').json()['total'] == counts['audit_errors']
    assert report['generated_at']


def test_empty_overview_is_known_zero_not_readiness(client, settings):
    authenticated(client, settings)
    report = client.get('/api/reports/collection').json()
    assert report['counts']['audio_files'] == report['counts']['current_proposals'] == report['counts']['published_current'] == 0
    assert 'readiness_percent' not in report
