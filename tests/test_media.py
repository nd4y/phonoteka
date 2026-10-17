"""R0: generated audio only; no network and no actual music collection."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, POPM, TXXX
from mutagen.mp3 import MP3
from PIL import Image

from phonoteka import media


LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Durable mutation profile requires Linux")
TOOLS = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")


@pytest.fixture
def roots(tmp_path):
    result = {key: tmp_path / key for key in ["source", "work", "publication"]}
    for path in result.values():
        path.mkdir()
    return result


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "state" / "backups"


@pytest.fixture
def make_audio(roots):
    def create(extension="mp3", name=None):
        target = roots["work"] / (name or f"synthetic.{extension}")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=3", "-c:a", "libmp3lame" if extension == "mp3" else "flac",
                        "-metadata", "title=Original", "-y", str(target)], check=True)
        if extension == "mp3":
            audio = MP3(target)
            audio.tags.add(TXXX(encoding=3, desc="PRIVATE_USER_TAG", text=["preserve me", "second value"]))
            audio.tags.add(POPM(email="owner@example.invalid", rating=193, count=12))
            audio.tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=["-4.00 dB"]))
            audio.save(v2_version=4)
        else:
            audio = FLAC(target)
            audio["PRIVATE_USER_TAG"] = ["preserve me", "second value"]
            audio["REPLAYGAIN_TRACK_GAIN"] = ["-4.00 dB"]
            audio.save()
        return target
    return create


def cover_bytes(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (30, 30), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_lrc_occurrences_offset_and_normalized_serialization():
    parsed = media.parse_lrc("\ufeff[offset:+500]\n[00:10][00:40]Припев\n[00:20.5]Куплет\n[01:01.010]")
    assert [line["start_ms"] for line in parsed["lines"]] == [9500, 20000, 39500, 60510]
    assert len({line["line_id"] for line in parsed["lines"]}) == 4
    assert media.serialize_lrc(parsed) == "[00:09.500]Припев\n[00:20.000]Куплет\n[00:39.500]Припев\n[01:00.510]"
    roundtrip = media.parse_lrc(media.serialize_lrc(parsed))
    assert [line["start_ms"] for line in roundtrip["lines"]] == [9500, 20000, 39500, 60510]


def test_lrc_preserves_equal_timestamps_empty_lines_and_long_minutes():
    document = media.parse_lrc("[120:02.1]A\n[120:02.10]B\n[120:02.100]")
    assert [row["text"] for row in document["lines"]] == ["A", "B", ""]
    assert media.serialize_lrc(document) == "[120:02.100]A\n[120:02.100]B\n[120:02.100]"


@pytest.mark.parametrize("value,code", [
    ("[offset:500]\n[00:00.1]early", "invalid_lyrics"),
    ("[00:61]bad", "invalid_lyrics"),
    ("[00:00]a<00:01.00>b", "enhanced_lrc"),
    ("[offset:1]\n[offset:1]", "invalid_lyrics"),
    ("[offset:x]", "invalid_lyrics"),
])
def test_lrc_invalid_input(value, code):
    with pytest.raises(media.MediaError) as failure:
        media.parse_lrc(value)
    assert failure.value.code == code


def test_lrc_partial_is_not_exported_as_complete():
    parsed = media.parse_lrc("[00:00.1]Timed\nMissing time")
    assert not parsed["complete"] and parsed["needs_placement"]
    assert parsed["lines"][1]["start_ms"] is None
    with pytest.raises(media.MediaError, match="Untimed"):
        media.serialize_lrc(parsed)
    assert media.serialize_lrc(parsed, allow_partial=True) == "[00:00.100]Timed"


def test_lrc_edit_does_not_silently_reorder():
    parsed = media.parse_lrc("[00:00.1]A\n[00:00.2]B")
    parsed["lines"][0]["start_ms"] = 300
    with pytest.raises(media.MediaError, match="silently reordered"):
        media.serialize_lrc(parsed)


@LINUX
@TOOLS
@pytest.mark.parametrize("extension", ["mp3", "flac"])
def test_apply_preserves_packets_foreign_tags_cover_and_rollback(make_audio, roots, backup_dir, extension):
    path = make_audio(extension)
    original = path.read_bytes()
    previous = media.inspect_audio(path, roots)
    identity = media.audio_identity(path)
    result = media.apply_changes(path, {"title": "Новое название", "artist": ["One", "Two"]},
                                 "[00:00.100]Первая\n[00:02.100]Вторая", previous["sha256"], backup_dir, roots,
                                 cover=cover_bytes())
    current = media.inspect_audio(path, roots)
    assert current["tags"]["title"] == ["Новое название"]
    assert current["tags"]["artist"] == ["One", "Two"]
    assert current["lrc"] == "[00:00.100]Первая\n[00:02.100]Вторая"
    assert current["has_art"] and not current["poweramp_verified"]
    assert media.read_art(path, roots) == (cover_bytes(), "image/png")
    assert media.audio_identity(path) == identity == result["audio_identity"]
    assert Path(result["backup_path"]).read_bytes() == original
    if extension == "mp3":
        tags = MP3(path).tags
        assert tags["TXXX:PRIVATE_USER_TAG"].text == ["preserve me", "second value"]
        assert tags["POPM:owner@example.invalid"].rating == 193
        assert tags["POPM:owner@example.invalid"].count == 12
        assert tags["TXXX:REPLAYGAIN_TRACK_GAIN"].text == ["-4.00 dB"]
    else:
        tags = FLAC(path)
        assert tags["PRIVATE_USER_TAG"] == ["preserve me", "second value"]
        assert tags["REPLAYGAIN_TRACK_GAIN"] == ["-4.00 dB"]
    restored = media.rollback(path, result["backup_path"], result["sha256"], roots)
    assert path.read_bytes() == original
    assert restored["sha256"] == previous["sha256"]
    assert media.audit_audio(path, roots)["state"] == "passed"


@LINUX
@TOOLS
def test_mutation_conflict_leaves_external_change(make_audio, roots, backup_dir):
    path = make_audio()
    before = media.sha256_file(path)
    with path.open("ab") as output:
        output.write(b"external change")
    changed = path.read_bytes()
    with pytest.raises(media.MediaError) as failure:
        media.apply_changes(path, {"title": "unsafe"}, None, before, backup_dir, roots)
    assert failure.value.code == "conflict"
    assert path.read_bytes() == changed


@LINUX
@TOOLS
def test_rollback_conflict_does_not_clobber_new_changes(make_audio, roots, backup_dir):
    path = make_audio()
    first = media.apply_changes(path, {"title": "first"}, None, media.sha256_file(path), backup_dir, roots)
    second = media.apply_changes(path, {"title": "second"}, None, media.sha256_file(path), backup_dir, roots)
    with pytest.raises(media.MediaError) as failure:
        media.rollback(path, first["backup_path"], first["sha256"], roots)
    assert failure.value.code == "conflict"
    assert media.sha256_file(path) == second["sha256"]


@LINUX
@TOOLS
def test_original_and_links_are_never_writable(make_audio, roots, backup_dir):
    path = make_audio()
    source = roots["source"] / "source.mp3"
    shutil.copyfile(path, source)
    with pytest.raises(media.MediaError):
        media.apply_changes(source, {"title": "unsafe"}, None, media.sha256_file(source), backup_dir, roots)
    symlink = roots["work"] / "symbol.mp3"
    symlink.symlink_to(source)
    with pytest.raises(media.MediaError):
        media.inspect_audio(symlink, roots)
    hardlink = roots["work"] / "hard.mp3"
    os.link(source, hardlink)
    with pytest.raises(media.MediaError):
        media.inspect_audio(hardlink, roots)


@LINUX
@TOOLS
def test_id3v23_is_read_only(make_audio, roots, backup_dir):
    path = make_audio()
    audio = MP3(path)
    audio.save(v2_version=3)
    before = path.read_bytes()
    assert not media.inspect_audio(path, roots)["write_supported"]
    with pytest.raises(media.MediaError):
        media.apply_changes(path, {"title": "unsupported"}, None, media.sha256_file(path), backup_dir, roots)
    assert path.read_bytes() == before


@LINUX
@TOOLS
def test_publish_create_collision_and_explicit_replacement(make_audio, roots, backup_dir):
    path = make_audio("flac")
    destination = roots["publication"] / "Ready" / "album" / path.name
    first = media.publish(path, destination, media.sha256_file(path), roots, roots["publication"], backup_dir=backup_dir)
    assert first["backup_path"] is None
    assert destination.read_bytes() == path.read_bytes()
    assert destination.stat().st_ino != path.stat().st_ino
    with pytest.raises(media.MediaError) as failure:
        media.publish(path, destination, media.sha256_file(path), roots, roots["publication"], backup_dir=backup_dir)
    assert failure.value.code == "conflict"
    old_bytes = destination.read_bytes()
    media.apply_changes(path, {"title": "v2"}, None, media.sha256_file(path), backup_dir, roots)
    second = media.publish(path, destination, media.sha256_file(path), roots, roots["publication"], first["sha256"], backup_dir=backup_dir)
    assert Path(second["backup_path"]).read_bytes() == old_bytes


@LINUX
@TOOLS
def test_atomic_no_replace_closes_publication_race(make_audio, roots, backup_dir):
    path = make_audio()
    destination = roots["publication"] / "Ready" / path.name
    def external_create():
        destination.write_bytes(b"another file")
    with pytest.raises(media.MediaError) as failure:
        media.publish(path, destination, media.sha256_file(path), roots, roots["publication"], backup_dir=backup_dir, authorize=external_create)
    assert failure.value.code == "conflict"
    assert destination.read_bytes() == b"another file"


@LINUX
@TOOLS
def test_recover_repeats_sync_after_rename_failure(make_audio, roots, backup_dir, monkeypatch):
    path = make_audio()
    before = media.sha256_file(path)
    original_sync = media._sync_directory
    def fail_target_sync(directory):
        if directory == path.parent:
            raise OSError("injected directory fsync failure")
        original_sync(directory)
    # Create stage directory first so injection happens after replace, not mkdir.
    (path.parent / ".phonoteka-staging").mkdir()
    monkeypatch.setattr(media, "_sync_directory", fail_target_sync)
    with pytest.raises(OSError):
        media.apply_changes(path, {"title": "committed bytes"}, None, before, backup_dir, roots)
    assert media.sha256_file(path) != before
    journal = next(backup_dir.glob("*.json"))
    assert json.loads(journal.read_text())["state"] == "replacing"
    with pytest.raises(OSError):
        media.recover(journal, roots)
    assert json.loads(journal.read_text())["state"] == "replacing"
    monkeypatch.setattr(media, "_sync_directory", original_sync)
    assert media.recover(journal, roots)["state"] == "committed"


@LINUX
@TOOLS
def test_recover_does_not_overwrite_unknown_revision(make_audio, roots, backup_dir):
    path = make_audio()
    result = media.apply_changes(path, {"title": "changed"}, None, media.sha256_file(path), backup_dir, roots)
    with path.open("ab") as output:
        output.write(b"unknown revision")
    actual = path.read_bytes()
    assert media.recover(result["journal_path"], roots)["state"] == "conflict"
    assert path.read_bytes() == actual


@LINUX
def test_writer_guard_rejects_second_worker(tmp_path):
    with media.writer_guard(tmp_path, "first"):
        with pytest.raises(media.MediaError) as failure:
            with media.writer_guard(tmp_path, "second"):
                pass
        assert failure.value.code == "storage_in_use"


@LINUX
@TOOLS
@pytest.mark.parametrize("extension", ["mp3", "flac"])
def test_other_picture_slots_are_preserved(make_audio, roots, backup_dir, extension):
    path = make_audio(extension)
    back = cover_bytes("blue")
    if extension == "mp3":
        audio = MP3(path)
        audio.tags.add(APIC(encoding=3, mime="image/png", type=4, desc="back", data=back))
    else:
        audio = FLAC(path)
        picture = Picture()
        picture.type, picture.mime, picture.data = 4, "image/png", back
        audio.add_picture(picture)
    audio.save()
    media.apply_changes(path, {}, None, media.sha256_file(path), backup_dir, roots, cover=cover_bytes())
    pictures = MP3(path).tags.getall("APIC") if extension == "mp3" else FLAC(path).pictures
    assert [image.data for image in pictures if image.type == 4] == [back]


@LINUX
@TOOLS
def test_revoked_authorization_prevents_replace(make_audio, roots, backup_dir):
    path = make_audio()
    before = path.read_bytes()
    def revoked():
        raise media.MediaError("authorization_revoked", "Grant revoked")
    with pytest.raises(media.MediaError, match="Grant revoked"):
        media.apply_changes(path, {"title": "bad"}, None, media.sha256_file(path), backup_dir, roots, authorize=revoked)
    assert path.read_bytes() == before
    assert next(backup_dir.glob("*.json")).exists()


@LINUX
@TOOLS
def test_rollback_journal_belongs_to_current_job(make_audio, roots, backup_dir):
    path = make_audio()
    original = path.read_bytes()
    first = media.apply_changes(path, {"title": "new"}, None, media.sha256_file(path), backup_dir / "first-job", roots)
    rollback_dir = backup_dir / "rollback-job"
    restored = media.rollback(path, first["backup_path"], first["sha256"], roots, backup_dir=rollback_dir)
    assert Path(restored["journal_path"]).parent == rollback_dir
    assert Path(restored["backup_path"]).parent == rollback_dir
    assert media.recover(restored["journal_path"], roots)["state"] == "committed"
    assert path.read_bytes() == original


@LINUX
@TOOLS
def test_publication_rollback_staging_is_outside_ready(make_audio, roots, backup_dir):
    source = make_audio()
    destination = roots["publication"] / "Ready" / source.name
    first = media.publish(source, destination, media.sha256_file(source), roots, roots["publication"], backup_dir=backup_dir / "publish-one")
    media.apply_changes(source, {"title": "second"}, None, media.sha256_file(source), backup_dir / "edit", roots)
    second = media.publish(source, destination, media.sha256_file(source), roots, roots["publication"], first["sha256"], backup_dir=backup_dir / "publish-two")
    rollback = media.rollback(destination, second["backup_path"], second["sha256"], roots, backup_dir=backup_dir / "rollback")
    document = json.loads(Path(rollback["journal_path"]).read_text())
    assert Path(document["staged"]).parent == roots["publication"] / ".staging"
    assert not list((roots["publication"] / "Ready").rglob(".phonoteka-staging"))


@LINUX
@TOOLS
def test_untouched_metadata_corruption_blocks_replace(make_audio, roots, backup_dir, monkeypatch):
    path = make_audio()
    before = path.read_bytes()
    original_edit = media._edit
    def corrupt_foreign_field(staged, tags, lrc, cover):
        original_edit(staged, tags, lrc, cover)
        altered = MP3(staged)
        altered.tags.delall("POPM")
        altered.save()
    monkeypatch.setattr(media, "_edit", corrupt_foreign_field)
    with pytest.raises(media.MediaError) as error:
        media.apply_changes(path, {"title": "test"}, None, media.sha256_file(path), backup_dir, roots)
    assert error.value.code == "metadata_changed"
    assert path.read_bytes() == before


@LINUX
@TOOLS
def test_packet_side_data_is_part_of_audio_identity(make_audio, monkeypatch):
    path = make_audio()
    original_process = media._process
    original = media.audio_identity(path)
    def change_gapless(filename, executable, arguments, **kwargs):
        result = original_process(filename, executable, arguments, **kwargs)
        payload = json.loads(result.stdout)
        assert payload["packets"][0].get("side_data_list"), "Generated MP3 must have gapless packet side data"
        payload["packets"][0]["side_data_list"][0]["skip_samples"] += 10
        result.stdout = json.dumps(payload).encode()
        return result
    monkeypatch.setattr(media, "_process", change_gapless)
    assert media.audio_identity(path) != original


@LINUX
@TOOLS
def test_audio_packet_without_hash_is_rejected(make_audio, monkeypatch):
    path = make_audio()
    original_process = media._process
    def drop_packet_hash(filename, executable, arguments, **kwargs):
        result = original_process(filename, executable, arguments, **kwargs)
        payload = json.loads(result.stdout)
        del payload["packets"][0]["data_hash"]
        result.stdout = json.dumps(payload).encode()
        return result
    monkeypatch.setattr(media, "_process", drop_packet_hash)
    with pytest.raises(media.MediaError, match="packets"):
        media.audio_identity(path)


def test_reservations_are_atomic_across_threads_and_mount_aliases(tmp_path, monkeypatch):
    first, second = tmp_path / "alias-one", tmp_path / "alias-two"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(media.shutil, "disk_usage", lambda _: SimpleNamespace(free=media._SPACE_MARGIN + 100))
    entered, release = threading.Event(), threading.Event()
    def reserve_first():
        with media._reserve_filesystem_space([(first, 60)]):
            entered.set()
            assert release.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(reserve_first)
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(media.MediaError) as failure:
                with media._reserve_filesystem_space([(second, 60)]):
                    pass
            assert failure.value.code == "insufficient_space"
        finally:
            release.set()
        running.result()
    assert not media._SPACE_RESERVED
    with media._reserve_filesystem_space([(second, 60)]):
        assert media._SPACE_RESERVED
    assert not media._SPACE_RESERVED


def test_space_reservation_sums_same_device_requests_before_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(media.shutil, "disk_usage", lambda _: SimpleNamespace(free=media._SPACE_MARGIN + 100))
    with pytest.raises(media.MediaError):
        with media._reserve_filesystem_space([(tmp_path, 60), (tmp_path, 60)]):
            pytest.fail("Two allocations on the same device exceeded the bound")
    assert not media._SPACE_RESERVED


def test_second_filesystem_failure_does_not_leak_first_reservation(monkeypatch):
    one = SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=111))
    two = SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=222))
    monkeypatch.setattr(media, "_existing_parent", lambda path: one if str(path) == "one" else two)
    monkeypatch.setattr(media.shutil, "disk_usage", lambda directory: SimpleNamespace(free=media._SPACE_MARGIN + (100 if directory is one else 10)))
    with pytest.raises(media.MediaError):
        with media._reserve_filesystem_space([(Path("one"), 60), (Path("two"), 60)]):
            pass
    assert not media._SPACE_RESERVED


@LINUX
@TOOLS
def test_unknown_id3_frames_survive_mutation(make_audio, roots, backup_dir):
    path = make_audio()
    audio = MP3(path)
    payload = b"opaque-user-frame"
    unknown = b"XABC" + bytes([0, 0, 0, len(payload)]) + b"\x00\x00" + payload
    audio.tags.unknown_frames.append(unknown)
    audio.save(v2_version=4)
    assert unknown in MP3(path).tags.unknown_frames
    media.apply_changes(path, {"title": "new"}, None, media.sha256_file(path), backup_dir, roots)
    assert unknown in MP3(path).tags.unknown_frames


@LINUX
@TOOLS
def test_insufficient_space_never_creates_staging(make_audio, roots, backup_dir, monkeypatch):
    path = make_audio()
    before = path.read_bytes()
    monkeypatch.setattr(media.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(media.MediaError) as failure:
        media.apply_changes(path, {"title": "new"}, None, media.sha256_file(path), backup_dir, roots)
    assert failure.value.code == "insufficient_space"
    assert path.read_bytes() == before
    assert not (path.parent / ".phonoteka-staging").exists()
    assert not backup_dir.exists()
