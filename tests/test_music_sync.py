"""Exercise the native publish job and the real filesystem replacement boundary."""
from dataclasses import replace
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from phonoteka import music_sync as sync
from phonoteka import db, media
from phonoteka.app import create_app
from test_backend import accept_publication_review, authenticated, drain, job, settings, synthetic


def run_job(client, headers, config, kind, ids=None, params=None):
    submitted = job(client, headers, kind, ids, params)
    drain(config)
    result = client.get("/api/jobs/" + submitted["id"]).json()
    assert result["state"] == "succeeded", result
    return result


class Scenario:
    def __init__(self, settings, client, identity_kind):
        self.settings = settings
        self.client = client
        self.headers = authenticated(client, settings)
        self.source = synthetic(settings)
        run_job(client, self.headers, settings, "scan")
        self.track = db.fetchone(settings.db_path, "SELECT id FROM tracks WHERE is_current=1")["id"]
        self.music = settings.state_dir.parent / "music"
        self.music.mkdir()
        self.state = settings.state_dir.parent / "sync-state"
        self.state.mkdir()
        info = self.music.stat()
        self.config = {"music": str(self.music), "ready": str(settings.publication_root / "Ready"),
                       "api_url": settings.app_url,
                       "music_identity": (sync.music_root_identity(self.music) if identity_kind == "persistent"
                                          else [info.st_dev, info.st_ino])}
        (self.state / "state.json").write_text(json.dumps({"schema": 1, "music_identity": self.config["music_identity"],
                                                           "tracks": {}, "conflicts": {}, "pending": None,
                                                           "delivered_count": 0, "queue": {}, "seen_jobs": []}), encoding="utf-8")

    def delivery(self):
        return sync.Delivery(self.config, self.state, self.client)

    def publish(self):
        accept_publication_review(self.client, self.headers, self.track)
        return run_job(self.client, self.headers, self.settings, "publish", [self.track])

    def apply(self):
        row = db.fetchone(self.settings.db_path, "SELECT sha256 FROM tracks WHERE id=?", (self.track,))
        response = self.client.post(f"/api/tracks/{self.track}/proposal",
                                    json={"tags": {"title": "Confirmed fixture"}},
                                    headers={**self.headers, "If-Match": '"' + row["sha256"] + '"'})
        assert response.status_code == 201, response.text
        run_job(self.client, self.headers, self.settings, "apply", [self.track])

    def initial_delivery(self):
        self.publish()
        delivery = self.delivery()
        assert delivery.cycle()["delivered_count"] == 1
        record = delivery.state["tracks"][self.track]
        return self.music / record["path"], record["sha256"]


@pytest.fixture(params=["legacy", "persistent"])
def scenario(settings, request):
    writable = replace(settings, read_only=False, storage_verified=True)
    with TestClient(create_app(writable), base_url=writable.app_url) as client:
        yield Scenario(writable, client, request.param)


def test_native_publication_replaces_only_after_confirmation(scenario):
    target, before = scenario.initial_delivery()
    scenario.apply()
    assert scenario.delivery().cycle()["delivered_count"] == 1
    assert media.sha256_file(target) == before
    scenario.publish()
    delivery = scenario.delivery()
    assert delivery.cycle()["delivered_count"] == 2
    record = delivery.state["tracks"][scenario.track]
    assert record["path"] == target.relative_to(scenario.music).as_posix()
    assert media.sha256_file(target) == record["sha256"] != before
    assert media.sha256_file(delivery.stage / record["backup"]) == before
    assert scenario.delivery().cycle()["delivered_count"] == 2
    result = db.fetchone(scenario.settings.db_path, "SELECT id FROM history WHERE track_id=? AND kind='publish' ORDER BY rowid DESC", (scenario.track,))
    run_job(scenario.client, scenario.headers, scenario.settings, "rollback_ready", [scenario.track],
            {"history_id": result["id"], "expected_ready_sha256": record["sha256"]})
    assert scenario.delivery().cycle()["delivered_count"] == 3
    assert media.sha256_file(target) == before


def test_untracked_ready_and_unsettled_publication_are_held(scenario):
    ready = scenario.settings.publication_root / "Ready"
    ready.mkdir(exist_ok=True)
    (ready / "untracked.flac").write_bytes(b"unapproved")
    assert scenario.delivery().cycle()["registered"] == 0
    submitted = scenario.publish()
    with db.transaction(scenario.settings.db_path) as conn:
        conn.execute("UPDATE jobs SET state='running' WHERE id=?", (submitted["id"],))
    result = scenario.delivery().cycle()
    assert result["registered"] == 0
    assert result["eligible_changes"] == 0
    assert not list(scenario.music.iterdir())


def test_drive_edit_is_preserved(scenario):
    target, _ = scenario.initial_delivery()
    target.write_bytes(b"edited through Drive")
    scenario.apply()
    scenario.publish()
    result = scenario.delivery().cycle()
    assert result["conflicts"][scenario.track]["code"] == "music_revision_conflict"
    assert result["delivered_count"] == 1
    assert target.read_bytes() == b"edited through Drive"


def test_drive_index_links_keep_old_and_new_inodes_readable(scenario, monkeypatch):
    target, before = scenario.initial_delivery()
    target.chmod(0o640)
    os.setxattr(target, "user.drive_test", b"keep access metadata")
    old_alias = scenario.music.parent / "drive-index-old"
    new_alias = scenario.music.parent / "drive-index-new"
    old_inode = target.stat().st_ino
    os.link(target, old_alias)
    scenario.apply()
    scenario.publish()
    exchange = sync.exchange

    def index_new_inode(source, destination):
        exchange(source, destination)
        os.link(destination, new_alias)

    monkeypatch.setattr(sync, "exchange", index_new_inode)
    delivery = scenario.delivery()
    result = delivery.cycle()
    assert result["delivered_count"] == 2 and not result["conflicts"] and not result["deferred"]
    record = delivery.state["tracks"][scenario.track]
    backup = delivery.stage / record["backup"]
    assert sync.stable_hash(backup)[0] == sync.stable_hash(old_alias)[0] == before
    assert backup.stat().st_ino == old_alias.stat().st_ino == old_inode
    assert sync.stable_hash(target)[0] == sync.stable_hash(new_alias)[0] == record["sha256"] != before
    assert target.stat().st_ino == new_alias.stat().st_ino != old_inode
    assert target.stat().st_mode & 0o777 == old_alias.stat().st_mode & 0o777 == 0o640
    assert os.getxattr(target, "user.drive_test") == os.getxattr(old_alias, "user.drive_test") == b"keep access metadata"


def test_race_restores_displaced_drive_edit(scenario, monkeypatch):
    target, _ = scenario.initial_delivery()
    scenario.apply()
    scenario.publish()
    original = sync.exchange
    calls = []
    def race(source, destination):
        if not calls:
            destination.write_bytes(b"Drive writer after compare")
        calls.append(True)
        original(source, destination)
    monkeypatch.setattr(sync, "exchange", race)
    result = scenario.delivery().cycle()
    assert len(calls) == 2
    assert result["conflicts"][scenario.track]["code"] == "music_changed_during_commit"
    assert target.read_bytes() == b"Drive writer after compare"


def test_restart_reconciles_commit_without_copying_again(scenario, monkeypatch):
    target, before = scenario.initial_delivery()
    scenario.apply()
    scenario.publish()
    delivery = scenario.delivery()
    def crash(_pending):
        raise SystemExit("interrupted after file commit")
    monkeypatch.setattr(delivery, "finish", crash)
    with pytest.raises(SystemExit):
        delivery.cycle()
    inode = target.stat().st_ino
    sha = media.sha256_file(target)
    assert sha != before
    assert scenario.delivery().cycle()["delivered_count"] == 2
    assert target.stat().st_ino == inode
    assert media.sha256_file(target) == sha


def test_missing_or_replaced_root_fails_closed(scenario):
    scenario.publish()
    (scenario.music.parent / "other").mkdir()
    scenario.config["music"] = str(scenario.music.parent / "other")
    with pytest.raises(sync.Deferred, match="music_root_changed"):
        scenario.delivery().cycle()


def test_persistent_identity_survives_device_renumbering(tmp_path, monkeypatch):
    identity = sync.music_root_identity(tmp_path)
    original = os.fstat

    def renumber(fd):
        info = original(fd)
        return SimpleNamespace(st_dev=info.st_dev + 1, st_ino=info.st_ino)

    monkeypatch.setattr(sync.os, "fstat", renumber)
    assert sync.music_root_identity(tmp_path) == identity


@pytest.mark.parametrize("field,value", [("fsid", "0000000000000001"), ("inode", "999999999999")])
def test_changed_persistent_identity_is_held(scenario, field, value):
    identity = sync.music_root_identity(scenario.music)
    assert identity[field] != value
    scenario.config["music_identity"] = {**identity, field: value}
    state_path = scenario.state / "state.json"
    state = json.loads(state_path.read_text())
    state["music_identity"] = scenario.config["music_identity"]
    state_path.write_text(json.dumps(state))
    with pytest.raises(sync.Deferred, match="music_root_changed"):
        scenario.delivery().check_root()


def test_legacy_identity_is_not_automatically_migrated(scenario):
    info = scenario.music.stat()
    scenario.config["music_identity"] = [info.st_dev + 1, info.st_ino]
    state_path = scenario.state / "state.json"
    state = json.loads(state_path.read_text())
    state["music_identity"] = scenario.config["music_identity"]
    state_path.write_text(json.dumps(state))
    with pytest.raises(sync.Deferred, match="music_root_changed"):
        scenario.delivery().check_root()
    assert json.loads(state_path.read_text()) == state


@pytest.mark.parametrize("fsid", [None, 0, -1, 2**64, "123"])
def test_unavailable_filesystem_identity_fails_closed(tmp_path, monkeypatch, fsid):
    monkeypatch.setattr(sync.os, "fstatvfs", lambda fd: SimpleNamespace(f_fsid=fsid))
    with pytest.raises(sync.Deferred, match="music_identity_unavailable"):
        sync.music_root_identity(tmp_path)


@pytest.mark.parametrize("value", [None, {}, [1], [True, 2], ["1", 2],
    {"kind": "unknown", "fsid": "0000000000000001", "inode": "1"},
    {"kind": sync.ROOT_IDENTITY, "fsid": "0000000000000000", "inode": "1"},
    {"kind": sync.ROOT_IDENTITY, "fsid": "0000000000000001", "inode": "01"}])
def test_malformed_identity_fails_closed(value):
    with pytest.raises(sync.Deferred, match="invalid_music_identity"):
        sync.validate_root_identity(value)


def test_symbolic_directory_identity_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        sync.music_root_identity(link)
