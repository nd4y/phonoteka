"""The guided journey runs against isolated real files using the public API."""
from dataclasses import replace
import hashlib
import io
import json
import zipfile

import pytest

from fastapi.testclient import TestClient

from phonoteka import db, preparation
from phonoteka.app import create_app
from test_backend import settings, authenticated, synthetic, seed, job, drain, accept_publication_review


@pytest.mark.parametrize("authenticated_mode", [True, False])
def test_folder_to_portable_download(settings, authenticated_mode):
    settings = replace(settings, read_only=False, storage_verified=True, external_metadata=False, external_fingerprints=False, auth_enabled=authenticated_mode)
    album=settings.work_root / "Артист_100%"
    album.mkdir()
    audio=synthetic(settings).rename(album / "Трек.flac")
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=(authenticated(client,settings) if authenticated_mode else {"Origin":settings.app_url, "X-CSRF-Token":client.get("/api/auth/session").json()["csrf_token"]})
        outside,_=seed(settings,"Соседний/не включать.mp3")
        old=db.fetchone(settings.db_path,"SELECT * FROM tracks WHERE id=?",(outside,))
        (settings.work_root / "link").symlink_to(settings.source_root,target_is_directory=True)
        listing=client.get("/api/work/folders").json()
        assert album.name in listing["directories"] and "link" not in listing["directories"]
        for unsafe in ("../original","/","link",album.name+"/.."):
            assert client.get("/api/work/folders",params={"folder":unsafe}).status_code==409
        body={"folder":album.name,"profile":"relaxed"}
        first=client.post("/api/preparation",json=body,headers={**headers,"Idempotency-Key":"run1"})
        assert first.status_code==202,first.text
        scan=first.json()["job_id"]
        assert client.post("/api/preparation",json=body,headers={**headers,"Idempotency-Key":"run1"}).json()["job_id"]==scan
        assert client.post("/api/preparation",json=body,headers={**headers,"Idempotency-Key":"other"}).status_code==409
        drain(settings)
        preparation.tick(settings)
        run=client.get("/api/preparation").json()
        assert run["state"]=="review" and run["skipped"]=={"recognize":"disabled","analyze":"disabled"},run
        assert db.fetchone(settings.db_path,"SELECT * FROM tracks WHERE id=?",(outside,))==old
        item=client.get("/api/jobs/"+scan).json()["results"][0]
        assert item["relative_path"]==album.name+"/"+audio.name
        assert [r["id"] for r in client.get("/api/tracks?filter=preparation").json()["items"]]==[item["catalog_id"]]
        track=item["catalog_id"]
        with db.connect(settings.db_path) as conn:
            selected=preparation.pending(conn,scan,"recognize","")
            assert [row["track_id"] for row in selected]==[track]
        assert client.get("/api/tracks/"+track+"/lyrics-review").json()["profile"]=="relaxed"
        assert client.get("/api/exports/latest").json()["ready_count"]==0
        blocked=client.get("/api/tracks/"+track+"/publication").json()
        assert "accepted_file_revision_required" in blocked["gate"]["reasons"]
        job(client,headers,"audit",[track]);drain(settings)
        accept_publication_review(client,headers,track)
        accepted=client.get("/api/publication/plan",params={"accepted_only":True}).json()
        assert [row["track_id"] for row in accepted["items"]]==[track]
        plan=client.post("/api/publications/plan",json={"track_ids":[track]},headers={**headers,"Idempotency-Key":"publish-plan"})
        assert plan.status_code==201,plan.text
        frozen=plan.json()
        committed=client.post("/api/publications/"+frozen["id"]+"/commit",json={"plan_sha256":frozen["plan_sha256"]},headers={**headers,"Idempotency-Key":"publish-selected"})
        assert committed.status_code==202,committed.text
        drain(settings)
        assert client.get("/api/jobs/"+committed.json()["id"]).json()["state"]=="succeeded"
        export=job(client,headers,"export_ready")
        assert job(client,headers,"export_ready")["id"]==export["id"]
        drain(settings)
        result=client.get("/api/jobs/"+export["id"]).json()
        assert result["state"]=="succeeded" and result["export_progress"]["copied"]==1,result
        latest=client.get("/api/exports/latest").json()
        doc=latest["export"]
        assert latest["ready_count"]==1 and result["results"][0]["export_id"]==doc["id"]
        response=client.get("/api/exports/"+doc["id"]+"/collection.zip")
        assert response.status_code==200 and response.headers["content-type"]=="application/zip"
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            entry=doc["files"][0];member="files/"+entry["path"]
            assert set(archive.namelist())=={"playlist.m3u8","manifest.json",member}
            assert "./"+member in archive.read("playlist.m3u8").decode()
            assert hashlib.sha256(archive.read(member)).hexdigest()==entry["sha256"]
        receipt=client.post("/api/deliveries",json={"export_id":doc["id"],"device":"fixture"},headers=headers)
        assert receipt.json()["state"]=="copy_reported"


def test_cancel_stops_pipeline_and_no_match_uses_current_revision(settings):
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings)
        run=client.post("/api/preparation",json={},headers={**headers,"Idempotency-Key":"cancel"}).json()
        client.post("/api/jobs/"+run["job_id"]+"/cancel",headers=headers)
        preparation.tick(settings)
        assert client.get("/api/preparation").json()["state"]=="stopped"
        current,sha=seed(settings,"current.mp3")
        stale,_=seed(settings,"stale.mp3")
        with db.transaction(settings.db_path) as conn:
            for tid,revision in ((current,sha),(stale,"0"*64)):
                conn.execute("INSERT INTO analyses VALUES(?,?,?,?)",(tid,revision,json.dumps({"state":"no_match","sources":[]}),db.utcnow()))
        assert [r["id"] for r in client.get("/api/tracks?filter=no_match").json()["items"]]==[current]

def test_stages_resume_once_with_frozen_folder_members(settings):
    settings=replace(settings,external_metadata=True,external_fingerprints=True)
    folder=settings.work_root/"test"
    folder.mkdir()
    synthetic(settings).rename(folder/"sample.flac")
    with TestClient(create_app(settings),base_url=settings.app_url) as client:
        headers=authenticated(client,settings)
        request=client.post("/api/preparation",json={"folder":"test"},headers={**headers,"Idempotency-Key":"sequence"}).json()
        drain(settings)
        for kind in ("recognize","analyze"):
            preparation.tick(settings)
            preparation.tick(settings)
            run=client.get("/api/preparation").json()
            assert run["stage"]==kind
            with db.transaction(settings.db_path) as conn:
                queued=conn.execute("SELECT payload,state FROM jobs WHERE id=?",(run["job_id"],)).fetchone()
                assert queued["state"]=="queued"
                assert [i["relative_path"] for i in json.loads(queued["payload"])["items"]]==["test/sample.flac"]
                assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind=?",(kind,)).fetchone()[0]==1
                # Simulate the terminal provider job receipt, without transmitting a fingerprint.
                conn.execute("UPDATE jobs SET state='succeeded',completed=1 WHERE id=?",(run["job_id"],))
        preparation.tick(settings)
        final=client.get("/api/preparation").json()
        assert final["completed"]==["scan","recognize","analyze"] and final["scan_id"]==request["scan_id"]
