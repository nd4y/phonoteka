"""Exercise the native notice pass without a DSM service or network."""
import hashlib
import json
import os
import time
from uuid import uuid4

import pytest

from phonoteka import drive_notify as notify
from phonoteka import music_sync


@pytest.fixture(params=["legacy", "persistent"])
def scenario(tmp_path, request):
    music = tmp_path / "Music"
    state = tmp_path / "notice-state"
    music.mkdir()
    state.mkdir()
    target = music / "confirmed.mp3"
    target.write_bytes(b"confirmed audio")
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    identity = music_sync.music_root_identity(music) if request.param == "persistent" else notify.identity(music)
    data = {"schema": 1, "music_identity": identity, "tracks": {
        "confirmed": {"path": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                      "publication_id": uuid4().hex, "job_id": str(uuid4()), "delivered_at": time.time() - 60},
        "seed": {"path": "prior.mp3", "sha256": "a" * 64},
    }}
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(data))
    return target, ledger, state


def test_uses_exact_delivery_and_never_repeats(scenario):
    target, ledger, state = scenario
    status = notify.run(target.parent, ledger, state)
    assert status["notified"] == 1 and status["pending"] == status["conflicts"] == 0
    assert target.read_bytes() == b"confirmed audio"
    mtime = target.stat().st_mtime_ns
    assert mtime > 1_000_000_000
    status = notify.run(target.parent, ledger, state)
    assert status["notified"] == status["pending"] == 0
    assert target.stat().st_mtime_ns == mtime


def test_foreign_edit_is_neither_overwritten_nor_notified(scenario):
    target, ledger, state = scenario
    target.write_bytes(b"external edit")
    mtime = target.stat().st_mtime_ns
    status = notify.run(target.parent, ledger, state)
    assert status["conflicts"] == 1 and status["notified"] == 0
    assert target.read_bytes() == b"external edit" and target.stat().st_mtime_ns == mtime


def test_root_identity_is_pinned(scenario):
    target, ledger, state = scenario
    wrong = target.parent.with_name("replacement")
    wrong.mkdir()
    with pytest.raises(ValueError, match="wrong_music_ledger"):
        notify.run(wrong, ledger, state)
    assert not list(state.iterdir())


def test_same_digest_symlink_is_held(scenario):
    target, ledger, state = scenario
    elsewhere = target.parent.with_name("other.mp3")
    target.rename(elsewhere)
    target.symlink_to(elsewhere)
    status = notify.run(target.parent, ledger, state)
    assert status["conflicts"] == 1 and status["notified"] == 0
    assert elsewhere.stat().st_mtime_ns == 1_000_000_000


def test_unknown_persistent_identity_is_rejected(scenario):
    target, ledger, state = scenario
    data = json.loads(ledger.read_text())
    data["music_identity"] = {"kind": "unknown", "fsid": "0000000000000001", "inode": "1"}
    ledger.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="wrong_music_ledger"):
        notify.run(target.parent, ledger, state)
    assert not list(state.iterdir())
