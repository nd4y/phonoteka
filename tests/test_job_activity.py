from datetime import datetime, timezone
import uuid

from starlette.requests import Request

from phonoteka import db, job_activity
from test_backend import authenticated, client, settings, seed


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def make_job(settings, *, state="running", total=10, completed=2, failed=0, stage="recognize", payload=None):
    identity = str(uuid.uuid4())
    now = db.utcnow()
    with db.transaction(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO jobs(id,kind,state,stage,payload,total,completed,failed,created_at,updated_at,auth_epoch) VALUES(?,'recognize',?,?,?,?,?,?,?,?,1)",
            (identity, state, stage, db.encode(payload or {}), total, completed, failed, now, now),
        )
    return db.fetchone(settings.db_path, "SELECT * FROM jobs WHERE id=?", (identity,))


def event(kind, at, **data):
    return {"kind": kind, "created_at": stamp(at), "data": data}


def test_eta_uses_only_active_segment_and_attempts(client, settings):
    row = make_job(settings, completed=3, failed=1)
    events = [
        event("job_created", 100), event("job_started", 1000),
        event("job_progress", 1010, completed=1, failed=0, state="succeeded"),
        event("job_progress", 1020, completed=2, failed=0, state="succeeded"),
        event("job_waiting", 1021), event("job_started", 5000),
        event("job_progress", 5010, completed=3, failed=0, state="succeeded"),
        event("job_progress", 5020, completed=3, failed=1, state="failed"),
    ]
    result = job_activity.estimate(row, events, now=5021)
    assert result["processed"] == 4 and result["progress"] == 40
    assert result["eta_seconds"] == 60 and result["recent_rate"] == 0.1
    assert result["progress_basis"] == "attempted_items"
    # A provider retry has to collect a fresh sample, rather than include a long wait.
    assert job_activity.estimate(row, events[:-1], now=5011)["eta_seconds"] is None


def test_eta_unknown_for_pause_wait_discovery_stall_and_finalization(client, settings):
    row = make_job(settings)
    events = [event("job_created", 999), event("job_started", 1000),
              event("job_progress", 1010, completed=1, failed=0, state="succeeded"),
              event("job_progress", 1020, completed=2, failed=0, state="succeeded")]
    cases = [
        ({"state": "queued"}, "queued"),
        ({"state": "paused"}, "paused"),
        ({"stage": "waiting_provider"}, "waiting_provider"),
        ({"stage": "waiting_storage"}, "waiting_storage"),
        ({"payload": db.encode({"pause_requested": True})}, "paused"),
        ({"cancel_requested": 1}, "cancelling"),
        ({"total": 0}, "unknown_total"),
        ({"total": 2}, "finalizing"),
    ]
    for changes, reason in cases:
        result = job_activity.estimate({**row, **changes}, events, now=1021)
        assert result["eta_seconds"] is None and result["eta_reason"] == reason
    result = job_activity.estimate(row, events, now=1200)
    assert result["eta_seconds"] is None and result["eta_reason"] == "no_recent_progress"
    assert job_activity.estimate(row, events[1:], now=1021)["eta_seconds"] is None
    assert job_activity.estimate(row, [*events, event("job_resume", 1021)], now=1022)["eta_seconds"] is None


def test_eta_does_not_treat_pruned_previous_progress_as_zero_baseline(client, settings):
    row=make_job(settings,total=20,completed=12)
    events=[event('job_started',1000),
            event('job_progress',1010,completed=11,failed=0,state='succeeded'),
            event('job_progress',1020,completed=12,failed=0,state='succeeded')]
    assert job_activity.estimate(row,events,now=1021)['eta_seconds'] is None
    events.append(event('job_progress',1030,completed=13,failed=0,state='succeeded'))
    result=job_activity.estimate({**row,'completed':13},events,now=1031)
    assert result['recent_rate']==.1 and result['eta_seconds']==70


def test_terminal_conflict_or_review_has_no_active_phase_or_wait_eta(client, settings):
    tid,_=seed(settings)
    for state in ('conflict','needs_review'):
        row=make_job(settings,state=state,stage='waiting_storage',completed=0,failed=1)
        with db.transaction(settings.db_path) as conn:
            conn.execute('INSERT INTO job_items VALUES(?,?,?,?)',(row['id'],tid,'running','{}'))
            db.event(conn,'job_action',{'job_id':row['id'],'track_id':tid,'action':'write_tags'})
        result=job_activity.snapshot(settings,row)
        assert result['active_items']==[] and result['summary']['active_count']==0
        assert result['summary']['eta_reason']=='finished' and result['summary']['eta_seconds'] is None
        assert ('Конфликт' if state=='conflict' else 'проверка') in result['summary']['current_action']


def test_active_phase_is_allowlisted_and_resets_on_new_attempt(client, settings):
    row=make_job(settings);tid,_=seed(settings)
    with db.transaction(settings.db_path) as conn:
        conn.execute('INSERT INTO job_items VALUES(?,?,?,?)',(row['id'],tid,'running','{}'))
        db.event(conn,'job_progress',{'job_id':row['id'],'track_id':tid,'state':'running'})
        db.event(conn,'job_action',{'job_id':row['id'],'track_id':tid,'action':'metadata_lookup','token':'private-value'})
    current=job_activity.snapshot(settings,row)
    assert current['active_items'][0]['action']=='Запрос тегов, обложки и текста'
    assert 'private-value' not in str(current)
    with db.transaction(settings.db_path) as conn:
        db.event(conn,'job_progress',{'job_id':row['id'],'track_id':tid,'state':'running'})
        db.event(conn,'job_action',{'job_id':row['id'],'track_id':tid,'action':['private-action']})
        db.event(conn,'job_action',{'job_id':row['id'],'track_id':{'private-id':'x'},'action':'commit'})
    current=job_activity.snapshot(settings,row)
    assert current['active_items'][0]['action']=='Распознавание музыки'
    assert 'private-action' not in str(current) and 'private-id' not in str(current)


def test_activity_is_private_bounded_and_projects_safe_fields(client, settings):
    row = make_job(settings, completed=1, failed=1)
    tid, _ = seed(settings, "nested/song.mp3")
    secret = "do-not-expose-provider-key"
    with db.transaction(settings.db_path) as conn:
        conn.execute("INSERT INTO job_items VALUES(?,?,?,?)", (row["id"], tid, "running", db.encode({"password": secret})))
        db.event(conn, "job_started", {"job_id": row["id"], "token": secret})
        for number in range(5):
            db.event(conn, "job_progress", {"job_id": row["id"], "track_id": tid, "state": "running", "completed": number, "failed": 0, "password": secret, "path": "/private/backup"})
        db.event(conn, "unrelated_private_event", {"job_id": row["id"], "password": secret})
    route = f"/api/jobs/{row['id']}/activity"
    assert client.get(route).status_code == 401
    assert client.get(f"/api/jobs/{row['id']}/events").status_code == 401
    authenticated(client, settings)
    response = client.get(route + "?limit=2")
    assert response.status_code == 200
    activity = response.json()
    assert response.headers["cache-control"] == "private, no-store"
    assert len(activity["events"]) == 2
    assert secret not in response.text and "/private/backup" not in response.text
    assert activity["active_items"] == [{"track_id": tid, "filename": "song.mp3", "action": "Распознавание музыки", "started_at": activity["events"][-1]["created_at"]}]
    assert activity["job"]["active_count"] == 1
    assert activity["job"]["processed"] == 2 and activity["job"]["progress"] == 20
    cursor = activity["cursor"]
    assert client.get(route + f"?after={cursor}").json()["events"] == []
    assert client.get(route + "?limit=201").status_code == 422
    assert client.get(route + "?after=-1").status_code == 422
    assert client.get("/api/v1/jobs/" + row["id"] + "/activity").status_code == 200
    assert client.get("/api/jobs/no-such-job/activity").status_code == 404


def test_activity_replay_detects_pruned_or_restored_cursor_and_preserves_order(client, settings):
    row = make_job(settings)
    authenticated(client, settings)
    with db.transaction(settings.db_path) as conn:
        for _ in range(8):
            db.event(conn, "job_started", {"job_id": row["id"]})
        conn.execute("DELETE FROM events WHERE id<4")
    route = f"/api/jobs/{row['id']}/activity"
    reset = client.get(route + "?after=1&limit=2").json()
    assert reset["resync_required"] and len(reset["events"]) == 2
    assert client.get(route + "?after=9999").json()["resync_required"]
    replay = client.get(route + "?after=4&limit=2").json()
    assert [item["id"] for item in replay["events"]] == [5, 6]
    assert replay["has_more"] and replay["cursor"] == 6
    last = client.get(route + "?after=6&limit=2").json()
    assert [item["id"] for item in last["events"]] == [7, 8]
    assert not last["has_more"] and last["cursor"] == 8


def test_job_list_eta_counts_errors_without_claiming_recognition(client, settings):
    row = make_job(settings, state="completed_with_errors", total=3, completed=1, failed=2)
    authenticated(client, settings)
    item = client.get("/api/jobs").json()["items"][0]
    assert item["id"] == row["id"] and item["progress"] == 100
    assert item["processed"] == 3 and item["failed"] == 2
    assert item["eta_seconds"] is None and item["eta_reason"] == "finished"
    assert item["active_count"] == 0 and item["state"] == "completed_with_errors"


def test_event_stream_filters_other_jobs_and_does_not_emit_raw_payload(client, settings, monkeypatch):
    row = make_job(settings)
    other = make_job(settings)
    authenticated(client, settings)
    tid, _ = seed(settings)
    with db.transaction(settings.db_path) as conn:
        db.event(conn, "job_started", {"job_id": other["id"], "password": "not-for-this-stream"})
        db.event(conn, "job_progress", {"job_id": row["id"], "track_id": tid, "state": "running", "password": "not-for-any-stream"})
    calls = 0
    async def disconnected(_request):
        nonlocal calls
        calls += 1
        return calls > 1
    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    response = client.get(f"/api/jobs/{row['id']}/events?after=1")
    assert response.status_code == 200 and response.headers["x-accel-buffering"] == "no"
    assert "event: job_activity" in response.text and '"filename":"a.mp3"' in response.text
    assert other["id"] not in response.text and "not-for-" not in response.text
    assert client.get(f"/api/jobs/{row['id']}/events", headers={"Last-Event-ID": "bad"}).status_code == 422


def test_malformed_event_values_are_ignored_and_scan_paths_are_reduced(client, settings):
    row = make_job(settings)
    malformed = {"id": 1, "kind": "job_progress", "created_at": db.utcnow(), "data": {"state": [], "track_id": {}, "password": "secret"}}
    assert job_activity.safe_event(malformed, row, {}, {})["message"] == "Обновление прогресса"
    assert job_activity.safe_event({**malformed,'kind':'job_action','data':{'action':[]}},row,{},{})['message']=='Действие обработки'
    scan = {**row, "kind": "scan"}
    projected = job_activity.safe_event({**malformed, "data": {"state": "failed", "track_id": "C:\\private\\album\\song.mp3"}}, scan, {}, {})
    assert projected["filename"] == "song.mp3" and "track_id" not in projected
