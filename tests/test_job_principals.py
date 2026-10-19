from fastapi.testclient import TestClient
from phonoteka import db
from phonoteka.app import create_app
from test_backend import settings, client, authenticated, seed


def test_idempotency_is_scoped_to_owner_and_each_token(client, settings):
    headers = authenticated(client, settings)
    tid, _ = seed(settings)
    tokens = [client.post('/api/tokens', json={'name':f'client-{i}', 'scopes':['read','jobs:analysis']}, headers=headers).json() for i in range(2)]
    body = {'kind':'audit','track_ids':[tid]}
    owner_job = client.post('/api/jobs', json=body, headers={**headers,'Idempotency-Key':'shared-key'}).json()
    jobs = []
    with TestClient(create_app(settings), base_url=settings.app_url) as external:
        for token in tokens:
            auth = {'Authorization':'Bearer '+token['token'], 'Idempotency-Key':'shared-key'}
            response = external.post('/api/jobs', json=body, headers=auth)
            assert response.status_code == 202, response.text
            jobs.append(response.json()['id'])
            assert external.post('/api/jobs', json=body, headers=auth).json()['id'] == jobs[-1]
            assert external.post('/api/jobs', json={**body,'params':{'generation':1}}, headers=auth).status_code == 409
    assert len({owner_job['id'], *jobs}) == 3
    for token, jid in zip(tokens, jobs):
        assert db.fetchone(settings.db_path, 'SELECT payload FROM jobs WHERE id=?',(jid,))['payload'].find(token['id']) >= 0
    assert client.delete('/api/tokens/'+tokens[0]['id'],headers=headers).status_code == 200
    waiting = client.get('/api/jobs/'+jobs[0]).json()
    assert waiting['id'] == jobs[0] and waiting['state'] == 'waiting_authorization'
    assert not waiting['cancel_requested'] and waiting['completed'] == waiting['failed'] == 0
    assert waiting['authorization']['reason'] == 'authorization_revoked'
    assert client.get('/api/jobs/'+jobs[1]).json()['state'] == 'queued'
    assert client.get('/api/jobs/'+owner_job['id']).json()['state'] == 'queued'


def test_cancel_waiting_job_preserves_partial_completion(client,settings):
    headers = authenticated(client,settings)
    tid, _ = seed(settings)
    response = client.post('/api/jobs',json={'kind':'audit','track_ids':[tid]},headers={**headers,'Idempotency-Key':'partial-cancel'})
    jid = response.json()['id']
    with db.transaction(settings.db_path) as conn:
        conn.execute("UPDATE jobs SET total=3,completed=1,failed=1,stage='waiting_storage' WHERE id=?",(jid,))
    response = client.post('/api/jobs/'+jid+'/cancel',headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result['state'] == result['stage'] == 'cancelled_partial'
    assert result['completed'] == result['failed'] == 1 and result['total'] == 3


def test_read_token_cannot_retry_storage_wait(client,settings):
    headers = authenticated(client,settings)
    tid, _ = seed(settings)
    jid = client.post('/api/jobs',json={'kind':'audit','track_ids':[tid]},headers={**headers,'Idempotency-Key':'storage-scope'}).json()['id']
    with db.transaction(settings.db_path) as conn:
        conn.execute("UPDATE jobs SET stage='waiting_storage' WHERE id=?",(jid,))
    token = client.post('/api/tokens',json={'name':'read-client','scopes':['read']},headers=headers).json()
    with TestClient(create_app(settings),base_url=settings.app_url) as external:
        assert external.post('/api/jobs/'+jid+'/retry-storage',headers={'Authorization':'Bearer '+token['token']}).status_code == 403
    assert client.get('/api/jobs/'+jid).json()['stage'] == 'waiting_storage'
