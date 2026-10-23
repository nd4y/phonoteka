import hashlib,json,threading,time
from fastapi.testclient import TestClient
from phonoteka import inference
from phonoteka.inference_budget import BudgetError

def test_running_cancel_is_durable_and_next_request_succeeds(tmp_path,monkeypatch):
    work=tmp_path/'work';work.mkdir();path=work/'test.wav';path.write_bytes(b'synthetic')
    key=tmp_path/'key';key.write_text('synthetic-inference-test-key')
    monkeypatch.setattr(inference,'ROOT',work);monkeypatch.setattr(inference,'STATE',tmp_path/'state');monkeypatch.setattr(inference,'SECRET',key)
    class FakeModel:
        def __init__(self):self.started=threading.Event()
        def run(self,body,*,cancel_event=None):
            self.started.set()
            if body['generation']==0:
                assert cancel_event.wait(5)
                raise BudgetError('inference_cancelled','Cancelled')
            return {'input_sha256':body['sha256'],'words':[],'resource_profile':inference.PROFILE}
        def request_stop(self):pass
        def close(self):pass
    model=FakeModel();monkeypatch.setattr(inference,'MODEL_WORKER',model)
    headers={'Authorization':'Bearer synthetic-inference-test-key'}
    body={'relative_path':'test.wav','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    with TestClient(inference.app) as client:
        submitted=client.post('/jobs',headers=headers,json=body);assert submitted.status_code==202
        identity=submitted.json()['id'];assert model.started.wait(3)
        response=client.delete('/jobs/'+identity,headers=headers)
        assert response.status_code==202 and response.json()['state']=='cancelling'
        for _ in range(60):
            state=client.get('/jobs/'+identity,headers=headers).json()
            if state['state']=='cancelled':break
            time.sleep(.02)
        assert state['state']=='cancelled' and state['result']['code']=='cancelled'
        assert client.post('/jobs',headers=headers,json=body).json()['id']==identity
        assert client.get('/jobs/'+identity,headers=headers).json()['state']=='cancelled'
        next_id=client.post('/jobs',headers=headers,json={**body,'generation':1}).json()['id']
        assert next_id!=identity
        for _ in range(80):
            next_state=client.get('/jobs/'+next_id,headers=headers).json()
            if next_state['state']=='succeeded':break
            time.sleep(.02)
        assert next_state['state']=='succeeded'
        # A completed job is never falsely reported cancelled.
        assert client.delete('/jobs/'+next_id,headers=headers).json()['state']=='succeeded'
