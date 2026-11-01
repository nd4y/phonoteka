import csv
import io
import json

from phonoteka import db
from test_backend import settings, client, authenticated, seed, job, drain


def test_report_contains_actual_file_outcomes_sources_and_exclusion_codes(client,settings):
    headers = authenticated(client,settings)
    tid, sha = seed(settings,relative='=private-track.mp3')
    pending = job(client,headers,'audit',[tid])
    drain(settings)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',(tid,sha,db.encode({
            'state':'no_match','sources':[{'provider':'lrclib','state':'budget_exhausted','message':'sensitive-token-must-not-export'}],
            'asr':{'text':'private unaccepted draft words'},'candidates':[]}),db.utcnow()))
    response=client.get('/api/reports/export/json')
    assert response.status_code == 200
    document=response.json()
    assert document['schema']==2 and len(document['files'])==1
    row=document['files'][0]
    assert row['id']==tid and row['analysis_state']=='no_match'
    assert json.loads(row['analysis_sources'])==[{'provider':'lrclib','state':'budget_exhausted'}]
    outcomes=json.loads(row['latest_outcomes'])
    assert outcomes[0]['job_id']==pending['id'] and outcomes[0]['state']=='failed'
    assert outcomes[0]['code']
    assert 'accepted_file_revision_required' in row['publication_reasons']
    assert row['publication_profile']=='lyrics_required' and not row['published_current_revision']
    assert 'sensitive-token' not in response.text and 'private unaccepted draft words' not in response.text
    assert json.loads(row['provenance'])['origin']=='preexisting'
    exported=list(csv.DictReader(io.StringIO(client.get('/api/reports/export/csv').text)))
    assert exported[0]['path'].startswith("'=")
    assert json.loads(exported[0]['latest_outcomes'])[0]['job_id']==pending['id']


def test_report_does_not_present_stale_analysis_as_current(client,settings):
    authenticated(client,settings)
    tid, _ = seed(settings)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO analyses VALUES(?,?,?,?)',(tid,'0'*64,db.encode({'state':'candidates','sources':[]}),db.utcnow()))
    row=client.get('/api/reports/export/json').json()['files'][0]
    assert row['analysis_state']=='stale' and json.loads(row['analysis_sources'])==[]
    assert json.loads(row['latest_outcomes'])==[] and row['audit_state']=='not_current'
