"""Watched-folder Takeout source: settings, discovery, and sync (app.folders).

Fully offline. The redirect cache is seeded for every title used, so the
database step never reaches for the network.

The properties under test, in order of how much they matter:
- no duplicate visits, however many times or routes an archive arrives by
- the user's files are never modified
- every failure lands in the report and on /health, never in an exception
"""

import json
import os
import sys
import zipfile

import pytest

from app import folders, localconfig, storage, sync, visitlog
from extract import extract_history

CAT = "https://en.wikipedia.org/wiki/Cat"
DOG = "https://en.wikipedia.org/wiki/Dog"
# Microseconds since 1601 (the extractor accepts either epoch).
T0 = 13_400_000_000_000_000


def history(*rows):
    return json.dumps({"Browser History": [
        {"url": url, "title": url.rsplit("/", 1)[-1], "time_usec": usec}
        for url, usec in rows
    ]})


def write_zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def takeout_zip(path, *rows):
    return write_zip(path, {"Takeout/Chrome/History.json": history(*rows)})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A scratch DB + log + sync state, local Chrome stubbed out, and the
    redirect cache seeded so ingest stays offline."""
    monkeypatch.setattr("config.SYNC_STATE_PATH", tmp_path / "sync_state.json")
    monkeypatch.setattr(
        sync, "sync_local_chrome",
        lambda **kwargs: {"profiles": [], "visits_logged": 0, "errors": []},
    )
    conn = storage.connect(tmp_path / "app.db")
    storage.update_redirect_cache(conn, {"Cat": "Cat", "Dog": "Dog"})
    conn.commit()
    conn.close()

    class Env:
        root = tmp_path
        db = tmp_path / "app.db"
        log = tmp_path / "visits.jsonl"

        def settings(self, *entries):
            settings = localconfig.load(tmp_path / "absent.toml")
            for entry in entries:
                path, label = entry if isinstance(entry, tuple) else (entry, "")
                localconfig.add_folder(settings, path, label)
            return settings

        def run(self, settings):
            return sync.run(db_path=self.db, settings=settings,
                            log_path=self.log, force=True)

        def visit_count(self):
            conn = storage.connect(self.db)
            try:
                return storage.count_visits(conn)
            finally:
                conn.close()

        def sources(self):
            conn = storage.connect(self.db)
            try:
                return storage.visit_source_summary(conn)
            finally:
                conn.close()

    return Env()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_folders_roundtrip_through_save_and_load(tmp_path):
    path = tmp_path / "config.local.toml"
    settings = localconfig.load(path)
    localconfig.add_folder(settings, tmp_path / "Personal Takeout", "Personal")
    localconfig.add_folder(settings, tmp_path / "work", "")
    localconfig.save(settings, path)

    reloaded = localconfig.watched_folders(localconfig.load(path))
    assert reloaded == [
        {"path": str((tmp_path / "Personal Takeout").resolve()), "label": "Personal"},
        {"path": str((tmp_path / "work").resolve()), "label": ""},
    ]
    assert "[[watched_folders]]" in path.read_text(encoding="utf-8")


def test_windows_style_paths_survive_the_toml_roundtrip(tmp_path):
    """Backslashes must be escaped on write and come back unchanged."""
    path = tmp_path / "config.local.toml"
    settings = localconfig.load(path)
    settings["watched_folders"] = [
        {"path": "C:\\Users\\Joe\\Google Drive\\Takeout", "label": "Personal"}
    ]
    localconfig.save(settings, path)
    raw = localconfig.load(path)["watched_folders"][0]["path"]
    assert raw == "C:\\Users\\Joe\\Google Drive\\Takeout"


def test_adding_a_folder_twice_updates_its_label(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    localconfig.add_folder(settings, tmp_path / "t", "Old")
    localconfig.add_folder(settings, str(tmp_path / "x" / ".." / "t"), "New")
    assert localconfig.watched_folders(settings) == [
        {"path": str((tmp_path / "t").resolve()), "label": "New"}
    ]


def test_remove_folder(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    localconfig.add_folder(settings, tmp_path / "a")
    localconfig.add_folder(settings, tmp_path / "b")
    localconfig.remove_folder(settings, tmp_path / "a")
    assert [f["path"] for f in localconfig.watched_folders(settings)] == [
        str((tmp_path / "b").resolve())
    ]


def test_hand_edited_bare_string_folder_is_tolerated(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    settings["watched_folders"] = [str(tmp_path / "t"), {"path": ""}, 7]
    assert localconfig.watched_folders(settings) == [
        {"path": str((tmp_path / "t").resolve()), "label": ""}
    ]


def test_legacy_sync_keys_move_into_the_sync_table(tmp_path):
    """The shape every existing config.local.toml has today."""
    path = tmp_path / "config.local.toml"
    path.write_text(
        "[takeout]\nenabled = true\nsync_on_launch = false\n"
        "min_sync_interval_hours = 12.0\n",
        encoding="utf-8",
    )
    settings = localconfig.load(path)
    assert localconfig.sync_on_launch(settings) is False
    assert localconfig.min_sync_interval_hours(settings) == 12.0

    localconfig.save(settings, path)
    text = path.read_text(encoding="utf-8")
    assert "sync_on_launch" not in text and "min_sync_interval_hours" not in text
    again = localconfig.load(path)
    assert localconfig.sync_on_launch(again) is False
    assert localconfig.min_sync_interval_hours(again) == 12.0


def test_legacy_off_flag_with_drive_disabled_does_not_stop_local_sync(tmp_path):
    """Disabling Takeout used to switch launch sync off too; that "off"
    meant "no Drive", not "stop capturing local Chrome history"."""
    path = tmp_path / "config.local.toml"
    path.write_text(
        "[takeout]\nenabled = false\nsync_on_launch = false\n", encoding="utf-8"
    )
    assert localconfig.sync_on_launch(localconfig.load(path)) is True


def test_an_explicit_sync_table_wins_over_legacy_keys(tmp_path):
    path = tmp_path / "config.local.toml"
    path.write_text(
        "[sync]\non_launch = true\nmin_interval_hours = 6\n\n"
        "[takeout]\nenabled = true\nsync_on_launch = false\n"
        "min_sync_interval_hours = 48\n",
        encoding="utf-8",
    )
    settings = localconfig.load(path)
    assert localconfig.sync_on_launch(settings) is True
    assert localconfig.min_sync_interval_hours(settings) == 6


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_scan_finds_takeout_zips_and_nothing_else(tmp_path):
    root = tmp_path / "watched"
    takeout_zip(root / "takeout-20260101T000000Z-001.zip", (CAT, T0))
    takeout_zip(root / "2026" / "Takeout-20260301T000000Z-001.ZIP", (CAT, T0))
    write_zip(root / "holiday-photos.zip", {"a.jpg": "x"})
    (root / "takeout-20260101T000000Z-002.tgz").write_bytes(b"x")
    (root / "takeout-20260101T000000Z-003.zip.crdownload").write_bytes(b"x")
    takeout_zip(root / ".Trash" / "takeout-old.zip", (CAT, T0))

    found = folders.scan(root).zips
    assert [p.name for p in found] == [
        "Takeout-20260301T000000Z-001.ZIP", "takeout-20260101T000000Z-001.zip",
    ]


def test_scan_stops_at_the_depth_limit(tmp_path):
    root = tmp_path / "watched"
    takeout_zip(root / "a" / "b" / "c" / "d" / "takeout-deep4.zip", (CAT, T0))
    takeout_zip(root / "a" / "b" / "c" / "d" / "e" / "takeout-deep5.zip", (CAT, T0))
    names = [p.name for p in folders.scan(root, max_depth=4).zips]
    assert names == ["takeout-deep4.zip"]


def test_scan_reports_a_missing_folder(tmp_path):
    with pytest.raises(folders.FolderUnavailable) as caught:
        folders.scan(tmp_path / "unplugged-drive")
    assert caught.value.status == "missing"


def test_scan_reports_a_file_given_as_a_folder(tmp_path):
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    with pytest.raises(folders.FolderUnavailable) as caught:
        folders.scan(tmp_path / "note.txt")
    assert caught.value.status == "unreadable"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="POSIX permissions; root ignores them")
def test_scan_reports_an_unreadable_folder(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(folders.FolderUnavailable) as caught:
            folders.scan(locked)
        assert caught.value.status == "unreadable"
    finally:
        locked.chmod(0o755)


def test_archive_key_tracks_path_size_and_mtime(tmp_path):
    zip_path = takeout_zip(tmp_path / "takeout-1.zip", (CAT, T0))
    file_id, modified = folders.archive_key(zip_path)
    assert file_id == f"file:{zip_path.resolve().as_posix()}"
    assert modified.endswith(f"|size={zip_path.stat().st_size}")
    assert folders.archive_key(zip_path) == (file_id, modified)  # stable

    os.utime(zip_path, (1_800_000_000, 1_800_000_000))
    assert folders.archive_key(zip_path)[1] != modified


def test_cloud_placeholder_detection_reads_platform_flags(monkeypatch):
    class Stat:
        st_flags = 0
        st_file_attributes = 0

    plain = Stat()
    assert folders.looks_cloud_only(plain) is False

    windows = Stat()
    windows.st_file_attributes = 0x00400000  # RECALL_ON_DATA_ACCESS
    assert folders.looks_cloud_only(windows) is True

    monkeypatch.setattr(folders.sys, "platform", "darwin")
    mac = Stat()
    mac.st_flags = 0x40000000  # SF_DATALESS
    assert folders.looks_cloud_only(mac) is True


# ---------------------------------------------------------------------------
# Sync: the seven required scenarios, plus the cross-route overlap
# ---------------------------------------------------------------------------

def test_new_zip_is_ingested_under_the_folder_label(env):
    folder = env.root / "watched"
    takeout_zip(folder / "takeout-1.zip", (CAT, T0), (DOG, T0 + 60_000_000))

    report = env.run(env.settings((folder, "Personal")))

    assert report["errors"] == []
    watched = report["folders"]
    assert watched["archives_processed"] == 1 and watched["visits_logged"] == 2
    assert {(r["source"], r["account"]) for r in visitlog.read(env.log)} == {
        ("takeout", "Personal")
    }
    assert env.visit_count() == 2


def test_unchanged_zip_is_skipped_on_the_next_sync(env):
    folder = env.root / "watched"
    takeout_zip(folder / "takeout-1.zip", (CAT, T0))
    settings = env.settings(folder)
    env.run(settings)

    second = env.run(settings)
    assert second["folders"]["archives_processed"] == 0
    assert second["folders"]["archives_skipped"] == 1
    assert env.visit_count() == 1


def test_modified_zip_is_reread_without_duplicating_visits(env):
    folder = env.root / "watched"
    zip_path = takeout_zip(folder / "takeout-1.zip", (CAT, T0))
    settings = env.settings(folder)
    env.run(settings)

    # Re-exported with one more visit (new size, new mtime).
    takeout_zip(zip_path, (CAT, T0), (DOG, T0 + 60_000_000))
    os.utime(zip_path, (1_900_000_000, 1_900_000_000))
    report = env.run(settings)

    assert report["folders"]["archives_processed"] == 1
    assert report["folders"]["visits_logged"] == 1      # only the new one
    assert env.visit_count() == 2
    assert len(visitlog.read(env.log)) == 2


def test_duplicate_copy_in_a_second_folder_adds_no_visits(env):
    takeout_zip(env.root / "home" / "takeout-1.zip", (CAT, T0))
    takeout_zip(env.root / "drive" / "takeout-1.zip", (CAT, T0))

    report = env.run(env.settings((env.root / "home", "Personal"),
                                  (env.root / "drive", "Drive copy")))

    assert report["folders"]["archives_processed"] == 2  # each read once
    assert env.visit_count() == 1                        # one physical visit
    # Both observations survive as provenance, without inflating counts.
    assert {(s["account"], s["visits"]) for s in env.sources()} == {
        ("Personal", 1), ("Drive copy", 1)
    }


def test_a_zip_under_two_overlapping_watched_folders_is_read_once(env):
    parent = env.root / "My Drive"
    takeout_zip(parent / "Takeout" / "takeout-1.zip", (CAT, T0))
    report = env.run(env.settings(parent, parent / "Takeout"))
    assert report["folders"]["archives_processed"] == 1
    assert report["folders"]["archives_skipped"] == 1


def test_non_takeout_zips_are_ignored_and_history_less_parts_are_benign(env):
    folder = env.root / "watched"
    write_zip(folder / "photos.zip", {"a.jpg": "x"})
    write_zip(folder / "takeout-1-002.zip", {"Takeout/Calendar/c.ics": "x"})
    settings = env.settings(folder)

    report = env.run(settings)
    status = report["folders"]["folders"][0]
    assert report["errors"] == []
    assert status["zips_found"] == 1           # photos.zip never considered
    assert status["ingested"] == 1 and status["status"] == "ok"
    # Recorded as processed, so it is not re-read every launch.
    assert env.run(settings)["folders"]["archives_skipped"] == 1


def test_corrupt_zip_is_reported_retried_and_never_fatal(env):
    folder = env.root / "watched"
    (folder).mkdir()
    (folder / "takeout-broken.zip").write_bytes(b"PK\x03\x04 this is not a zip")
    takeout_zip(folder / "takeout-good.zip", (CAT, T0))
    settings = env.settings((folder, "Personal"))

    report = env.run(settings)
    status = report["folders"]["folders"][0]
    assert report["finished_at"]                     # the pass completed
    assert status["status"] == "error" and status["failed"] == 1
    assert status["ingested"] == 1                   # the good one still landed
    assert any("takeout-broken.zip" in e and "not a readable zip" in e
               for e in report["errors"])
    # Not marked processed: it is tried again next time.
    again = env.run(settings)["folders"]["folders"][0]
    assert again["failed"] == 1 and again["skipped"] == 1


def test_missing_folder_is_reported_and_other_folders_still_sync(env):
    good = env.root / "good"
    takeout_zip(good / "takeout-1.zip", (CAT, T0))
    report = env.run(env.settings((env.root / "unplugged", "USB"), good))

    statuses = {f["path"]: f["status"] for f in report["folders"]["folders"]}
    assert statuses[str((env.root / "unplugged").resolve())] == "missing"
    assert statuses[str(good.resolve())] == "ok"
    assert any("USB" in e and "not found" in e for e in report["errors"])
    assert env.visit_count() == 1


def test_offline_cloud_placeholder_is_an_expected_recorded_failure(env, monkeypatch):
    folder = env.root / "watched"
    takeout_zip(folder / "takeout-1.zip", (CAT, T0))

    def offline(path):
        raise TimeoutError(60, "Operation timed out")

    monkeypatch.setattr(extract_history, "load_takeout_zip", offline)
    monkeypatch.setattr(folders, "looks_cloud_only", lambda stat: True)
    report = env.run(env.settings(folder))

    assert report["finished_at"]
    message = " ".join(report["errors"])
    assert "online-only Google Drive file" in message
    assert "available offline" in message


def test_zips_are_never_modified(env):
    folder = env.root / "watched"
    zip_path = takeout_zip(folder / "takeout-1.zip", (CAT, T0))
    before = (zip_path.read_bytes(), zip_path.stat().st_mtime_ns)
    env.run(env.settings(folder))
    assert (zip_path.read_bytes(), zip_path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in folder.iterdir()) == ["takeout-1.zip"]


def test_same_archive_via_drive_and_a_folder_is_one_visit(env, monkeypatch):
    """The existing dedup covers the cross-route case with no new code: the
    log keeps both records (different account), the DB keeps one visit."""
    import shutil

    from app import drive

    archive = takeout_zip(env.root / "download" / "takeout-1.zip", (CAT, T0))
    monkeypatch.setattr(
        drive, "download_archive",
        lambda creds, file_id, dest: shutil.copy(archive, dest) and dest,
    )
    conn = storage.connect(env.db)
    try:
        sync._ingest_one_archive(
            conn, creds=None,
            archive={"id": "drive-file-1", "name": "takeout-1.zip",
                     "modifiedTime": "2026-01-01T00:00:00Z"},
            email="me@example.com", log_path=env.log,
        )
    finally:
        conn.close()

    report = env.run(env.settings((env.root / "download", "Personal")))

    assert report["errors"] == []
    assert len(visitlog.read(env.log)) == 2
    assert env.visit_count() == 1
    assert {s["account"] for s in env.sources()} == {"me@example.com", "Personal"}


def test_no_folders_configured_is_reported_as_skipped(env):
    report = env.run(env.settings())
    assert report["folders"] == {"skipped": "No watched folders configured"}


def test_folder_status_reaches_sync_state_and_health(env):
    from fastapi.testclient import TestClient

    from app import api

    takeout_zip(env.root / "good" / "takeout-1.zip", (CAT, T0))
    env.run(env.settings((env.root / "good", "Personal"), env.root / "gone"))

    state = sync.load_state()
    assert [f["status"] for f in state["folders"]] == ["ok", "missing"]

    app = api.create_app(db_path=env.db, reference_dir=env.root / "no_reference")
    with TestClient(app) as client:
        folders_health = client.get("/health").json()["sync"]["folders"]
    assert folders_health[0]["label"] == "Personal"
    assert folders_health[0]["zips_found"] == 1
    assert folders_health[1]["status"] == "missing"
    assert folders_health[1]["errors"]
