"""Unattended intake waits for independent stable scans, not one quiet read."""
from dataclasses import replace
import json

from phonoteka import db, input_readiness, scheduling
from phonoteka.worker import Worker
from test_backend import settings, client, authenticated, synthetic, job, drain


def metadata(size=100, mtime=10, revision='a'*64):
    return {'size':size,'mtime_ns':mtime,'sha256':revision}


def pass_scan(meta, previous, identity, at, complete=True):
    meta={**meta,'input_readiness':input_readiness.observed(meta,previous,identity,now=at)}
    meta['input_readiness']=input_readiness.confirm(meta,identity,complete=complete,grace_seconds=60,now=at)
    return meta


def test_two_distinct_scans_and_grace_both_required():
    meta=metadata()
    assert input_readiness.describe(meta,grace_seconds=60,now=100)['state']=='waiting_stability'
    first=pass_scan(meta,{},'first',100)
    same=pass_scan(meta,first,'first',200)
    assert same['input_readiness']['confirmations']==1
    second=pass_scan(meta,first,'second',120)
    assert input_readiness.describe(second,grace_seconds=60,now=159)['state']=='waiting_stability'
    assert input_readiness.describe(second,grace_seconds=60,now=160)['state']=='ready'
    assert input_readiness.describe(second,grace_seconds=60,now=90)['state']=='waiting_stability'


def test_stat_revision_or_partial_scan_resets_prior_readiness():
    old=pass_scan(metadata(),{},'first',100)
    old=pass_scan(metadata(),old,'second',160)
    assert input_readiness.describe(old,grace_seconds=60,now=160)['state']=='ready'
    for changed in (metadata(size=101),metadata(mtime=11),metadata(revision='b'*64)):
        fresh=pass_scan(changed,old,'third',170)
        assert fresh['input_readiness']['confirmations']==1
        assert input_readiness.describe(fresh,grace_seconds=0,now=999)['state']=='waiting_stability'
    interrupted=pass_scan(metadata(),old,'third',170,complete=False)
    assert interrupted['input_readiness']['confirmations']==0
    assert input_readiness.describe(interrupted,grace_seconds=0,now=999)['state']=='waiting_stability'
    assert input_readiness.describe({'input_readiness':[]},grace_seconds=0)['state']=='waiting_stability'
    assert input_readiness.signature(metadata(revision='z'*64)) is None
    assert input_readiness.signature(metadata(revision='A'*64)) is None


def test_scan_confirms_only_successful_coverage_and_unseen_rows_reset(settings,client,monkeypatch):
    headers=authenticated(client,settings)
    path=synthetic(settings)
    for _ in range(2):
        job(client,headers,'scan');drain(settings)
    row=db.fetchone(settings.db_path,'SELECT * FROM tracks WHERE is_current=1')
    assert json.loads(row['metadata'])['input_readiness']['confirmations']==2
    # An inaccessible directory makes the scan partial. Its previous catalog
    # entries remain visible, but no old "ready" stamp authorizes downstream.
    monkeypatch.setattr('phonoteka.worker.enumerate_audio',lambda root:([],[{'code':'directory_unavailable'}]))
    job(client,headers,'scan');drain(settings)
    row=db.fetchone(settings.db_path,'SELECT * FROM tracks WHERE id=?',(row['id'],))
    assert row['status']!='missing'
    assert json.loads(row['metadata'])['input_readiness']['confirmations']==0
    assert path.exists()


def test_schedule_waits_for_legacy_or_unstable_rows_but_owner_job_is_explicit(settings,client):
    headers=authenticated(client,settings)
    synthetic(settings)
    job(client,headers,'scan');drain(settings)
    cfg=replace(settings,external_fingerprints=True,input_grace_seconds=0)
    schedule={'enabled':True,'steps':['recognize'],'remaining':[],'auth_epoch':int(db.setting(cfg.db_path,'auth_epoch')),
              'next_run':0,'interval_minutes':30}
    db.set_setting(cfg.db_path,'schedule',db.encode(schedule))
    scheduling.tick(cfg)
    assert db.fetchone(cfg.db_path,"SELECT id FROM jobs WHERE kind='recognize'") is None
    state=json.loads(db.setting(cfg.db_path,'schedule'))
    assert state['waiting_inputs']==1 and state['waiting_reason']=='input_stabilizing'
    # The background gate does not invent an error or silently prohibit an
    # explicitly requested local owner audit of that exact current revision.
    row=db.fetchone(cfg.db_path,'SELECT id FROM tracks WHERE is_current=1')
    audit=job(client,headers,'audit',[row['id']]);drain(settings)
    assert client.get('/api/jobs/'+audit['id']).json()['state']=='succeeded'
    db.set_setting(cfg.db_path,'schedule',db.encode({**state,'enabled':False}))


def test_config_grace_has_bounded_explicit_environment(settings,monkeypatch):
    import pytest
    from phonoteka.config import Settings
    monkeypatch.setenv('INPUT_GRACE_SECONDS','125')
    assert Settings.from_env().input_grace_seconds==125
    with pytest.raises(ValueError):replace(settings,input_grace_seconds=-1)
