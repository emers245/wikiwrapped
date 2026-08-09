"""Takeout archive parsing, local config, and sync resilience (D10, D12).

Fully offline. Nothing here touches Google: the Drive layer is exercised
only for its failure modes, which is the part that has to behave when the
network, the token, or the archive is not what it should be.

The load-bearing property under test is that a broken Takeout setup can
never stop the dashboard from serving. Every failure below has to end up in
a report, not an exception.
"""

import json
import zipfile

import pytest

from app import drive, localconfig, sync, visitlog
from extract import extract_history as eh

CAT_URL = "https://en.wikipedia.org/wiki/Cat"
# Microseconds since 1601-01-01 (the Chromium epoch), not Unix.
CAT_USEC = 13_400_000_000_000_000


def write_archive(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def takeout_payload(rows=None):
    return json.dumps({
        "Browser History": rows if rows is not None else [
            {"url": CAT_URL, "title": "Cat", "time_usec": CAT_USEC},
            {"url": "https://www.google.com/search?q=cat", "title": "cat",
             "time_usec": CAT_USEC + 1},
        ]
    })


# ---------------------------------------------------------------------------
# Archive parsing
# ---------------------------------------------------------------------------

def test_reads_history_from_a_takeout_archive(tmp_path):
    archive = write_archive(tmp_path / "takeout-001.zip", {
        "Takeout/Chrome/History.json": takeout_payload(),
        "Takeout/Chrome/Bookmarks.html": "<html></html>",
        "Takeout/archive_browser.html": "<html></html>",
    })
    visits, counts = eh.build_visits(eh.load_takeout_zip(archive))

    # Non-Wikipedia rows are discarded at the door and never persisted.
    assert counts["read"] == 2
    assert counts["non_wikipedia"] == 1
    assert [v["url"] for v in visits] == [CAT_URL]
    # 13.4e15 us since 1601 is 2025-08-18 in Unix terms.
    assert visits[0]["timestamp"] == "2025-08-18T14:13:20Z"


def test_accepts_the_older_browserhistory_name(tmp_path):
    archive = write_archive(tmp_path / "t.zip", {
        "Takeout/Chrome/BrowserHistory.json": takeout_payload(),
    })
    assert len(eh.load_takeout_zip(archive)) == 2


def test_prefers_the_shallowest_matching_member(tmp_path):
    archive = write_archive(tmp_path / "t.zip", {
        "Takeout/Chrome/History.json": takeout_payload(),
        "Takeout/Chrome/Nested/Deeper/History.json": takeout_payload([]),
    })
    assert eh.find_takeout_history_member(
        zipfile.ZipFile(archive)
    ) == "Takeout/Chrome/History.json"


def test_archive_without_chrome_history_raises_the_benign_error(tmp_path):
    """One part of a multi-part export carries no history; that is normal."""
    archive = write_archive(tmp_path / "t.zip", {"Takeout/Calendar/cal.ics": "x"})
    with pytest.raises(eh.NoHistoryInArchive):
        eh.load_takeout_zip(archive)


def test_history_part_missing_is_recorded_not_warned(scratch, monkeypatch):
    """A no-history part must be marked processed, or it downloads forever."""
    from app import storage

    archive = write_archive(scratch / "part.zip", {"Takeout/Calendar/c.ics": "x"})
    monkeypatch.setattr(
        drive, "download_archive",
        lambda creds, file_id, dest: __import__("shutil").copy(archive, dest) or dest,
    )
    conn = storage.connect(scratch / "app.db")
    try:
        added = sync._ingest_one_archive(
            conn, creds=None,
            archive={"id": "f1", "name": "part.zip",
                     "modifiedTime": "2026-01-01T00:00:00Z"},
            email="a@example.com", log_path=scratch / "visits.jsonl",
        )
        assert added == 0
        # Recorded, so the next sync skips it instead of re-downloading.
        assert storage.is_archive_processed(conn, "f1", "2026-01-01T00:00:00Z")
    finally:
        conn.close()


def test_changed_json_structure_fails_loudly(tmp_path):
    archive = write_archive(tmp_path / "t.zip", {
        "Takeout/Chrome/History.json": json.dumps({"Surprise": {"a": 1}}),
    })
    with pytest.raises(ValueError, match="Browser History"):
        eh.load_takeout_zip(archive)


def test_rows_without_a_url_field_fail_loudly(tmp_path):
    archive = write_archive(tmp_path / "t.zip", {
        "Takeout/Chrome/History.json": json.dumps(
            {"Browser History": [{"page": "x", "time_usec": CAT_USEC}]}
        ),
    })
    with pytest.raises(ValueError, match="no row carries a 'url' field"):
        eh.load_takeout_zip(archive)


def test_bare_list_export_still_parses(tmp_path):
    archive = write_archive(tmp_path / "t.zip", {
        "Takeout/Chrome/History.json": json.dumps(
            [{"url": CAT_URL, "title": "Cat", "time_usec": CAT_USEC}]
        ),
    })
    assert len(eh.load_takeout_zip(archive)) == 1


def test_extractor_cli_detects_a_zip_by_magic(tmp_path):
    archive = write_archive(tmp_path / "takeout.zip", {
        "Takeout/Chrome/History.json": takeout_payload(),
    })
    out = tmp_path / "out.json"
    assert eh.main(["--input", str(archive), "--output", str(out)]) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["schema_version"] == "1.0"
    assert [v["url"] for v in document["visits"]] == [CAT_URL]


# ---------------------------------------------------------------------------
# Local config
# ---------------------------------------------------------------------------

def test_missing_config_defaults_to_disabled(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    assert localconfig.takeout_enabled(settings) is False
    assert localconfig.sync_on_launch(settings) is False
    assert localconfig.account_emails(settings) == []


def test_config_roundtrips_through_save_and_load(tmp_path):
    path = tmp_path / "config.local.toml"
    settings = localconfig.load(path)
    settings["takeout"]["enabled"] = True
    settings["takeout"]["sync_on_launch"] = True
    settings["takeout"]["min_sync_interval_hours"] = 12
    localconfig.add_account(settings, "a@example.com")
    localconfig.add_account(settings, "b@example.com")
    localconfig.save(settings, path)

    reloaded = localconfig.load(path)
    assert localconfig.takeout_enabled(reloaded)
    assert localconfig.sync_on_launch(reloaded)
    assert localconfig.min_sync_interval_hours(reloaded) == 12
    assert localconfig.account_emails(reloaded) == ["a@example.com", "b@example.com"]


def test_setup_is_rerunnable_and_preserves_unknown_keys(tmp_path):
    """A second setup run must add an account, not wipe the file."""
    path = tmp_path / "config.local.toml"
    settings = localconfig.load(path)
    localconfig.add_account(settings, "a@example.com")
    settings["takeout"]["future_option"] = "keep me"
    localconfig.save(settings, path)

    again = localconfig.load(path)
    localconfig.add_account(again, "b@example.com")
    localconfig.save(again, path)

    final = localconfig.load(path)
    assert localconfig.account_emails(final) == ["a@example.com", "b@example.com"]
    assert final["takeout"]["future_option"] == "keep me"


def test_adding_an_existing_account_is_a_noop(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    localconfig.add_account(settings, "a@example.com")
    localconfig.add_account(settings, "a@example.com")
    assert localconfig.account_emails(settings) == ["a@example.com"]


def test_remove_account(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    localconfig.add_account(settings, "a@example.com")
    localconfig.add_account(settings, "b@example.com")
    localconfig.remove_account(settings, "a@example.com")
    assert localconfig.account_emails(settings) == ["b@example.com"]


def test_sync_on_launch_requires_takeout_enabled(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    settings["takeout"]["sync_on_launch"] = True
    settings["takeout"]["enabled"] = False
    assert localconfig.sync_on_launch(settings) is False


def test_token_path_is_per_account_and_filename_safe():
    path = localconfig.token_path("a.b+c@example.com")
    assert path.name == "a.b_c@example.com.json"
    assert path.parent.name == "tokens"


def test_config_quoting_survives_awkward_values(tmp_path):
    path = tmp_path / "c.toml"
    settings = localconfig.load(path)
    settings["takeout"]["drive_folder_id"] = 'has "quotes" and \\ backslash'
    localconfig.save(settings, path)
    assert localconfig.load(path)["takeout"]["drive_folder_id"] == (
        'has "quotes" and \\ backslash'
    )


# ---------------------------------------------------------------------------
# Drive failure modes: the app must survive all of them
# ---------------------------------------------------------------------------

def test_missing_token_asks_for_reauthorization_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr("config.TOKEN_DIR", tmp_path / "tokens")
    if not drive.libraries_available():
        pytest.skip("google libraries not installed")
    with pytest.raises(drive.ReauthorizationNeeded) as caught:
        drive.load_credentials("missing@example.com")
    assert caught.value.account == "missing@example.com"
    assert "missing@example.com" in str(caught.value)


def test_reauthorization_error_names_the_account_and_the_fix():
    error = drive.ReauthorizationNeeded("someone@example.com", "token expired")
    assert error.account == "someone@example.com"
    assert "someone@example.com" in str(error)
    assert "setup_takeout.py --authorize" in str(error)


def test_drive_error_when_libraries_absent(monkeypatch):
    """An install without the Google packages explains itself."""
    monkeypatch.setattr(drive, "_google_modules", _raise_missing)
    assert drive.libraries_available() is False


def _raise_missing():
    raise drive.DriveError(drive.INSTALL_HINT)


# ---------------------------------------------------------------------------
# Sync: never blocks or breaks startup
# ---------------------------------------------------------------------------

@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr("config.SYNC_STATE_PATH", tmp_path / "sync_state.json")
    return tmp_path


def disabled_settings():
    settings = localconfig.load(_missing := "/nonexistent/config.toml")
    return settings


def test_sync_with_takeout_disabled_still_reads_local(scratch, monkeypatch):
    monkeypatch.setattr(
        sync, "sync_local_chrome",
        lambda **kwargs: {"profiles": [], "visits_logged": 0, "errors": []},
    )
    report = sync.run(
        db_path=scratch / "app.db",
        settings=disabled_settings(),
        log_path=scratch / "visits.jsonl",
        force=True,
    )
    assert report["errors"] == []
    assert report["takeout"] == {"skipped": "Takeout integration is disabled"}


def test_sync_survives_a_failing_local_read(scratch, monkeypatch):
    def explode(path):
        raise OSError("Chrome History is locked")

    monkeypatch.setattr(sync.extract_history, "load_chrome_db", explode)
    monkeypatch.setattr(
        sync.extract_history, "find_chrome_profiles",
        lambda only=None: [{"directory": "Default", "display_name": "Person 1",
                            "account": "", "path": scratch / "History"}],
    )
    report = sync.run(
        db_path=scratch / "app.db",
        settings=disabled_settings(),
        log_path=scratch / "visits.jsonl",
        force=True,
    )
    # Recorded, not raised.
    assert any("locked" in message for message in report["errors"])
    assert report["finished_at"]


def test_sync_survives_a_reauthorization_failure(scratch, monkeypatch):
    settings = disabled_settings()
    settings["takeout"]["enabled"] = True
    localconfig.add_account(settings, "a@example.com")

    def needs_reauth(email, settings=None):
        raise drive.ReauthorizationNeeded(email, "refresh token expired")

    monkeypatch.setattr(drive, "load_credentials", needs_reauth)
    monkeypatch.setattr(
        sync, "sync_local_chrome",
        lambda **kwargs: {"profiles": [], "visits_logged": 0, "errors": []},
    )
    report = sync.run(
        db_path=scratch / "app.db",
        settings=settings,
        log_path=scratch / "visits.jsonl",
        force=True,
    )
    assert report["reauthorize"] == ["a@example.com"]
    assert any("a@example.com" in message for message in report["errors"])
    assert report["finished_at"]  # the pass still completed


def test_sync_reports_when_no_account_is_authorized(scratch, monkeypatch):
    settings = disabled_settings()
    settings["takeout"]["enabled"] = True
    monkeypatch.setattr(
        sync, "sync_local_chrome",
        lambda **kwargs: {"profiles": [], "visits_logged": 0, "errors": []},
    )
    report = sync.run(
        db_path=scratch / "app.db",
        settings=settings,
        log_path=scratch / "visits.jsonl",
        force=True,
    )
    assert any("no account is authorized" in m for m in report["errors"])


def test_launch_gate_skips_when_configured_off(scratch):
    report = sync.run(
        db_path=scratch / "app.db",
        settings=disabled_settings(),
        log_path=scratch / "visits.jsonl",
        on_launch=True,
    )
    assert report["skipped"] is True
    assert "sync_on_launch" in report["reason"]


def test_recent_sync_early_exits(scratch, monkeypatch):
    monkeypatch.setattr(
        sync, "sync_local_chrome",
        lambda **kwargs: {"profiles": [], "visits_logged": 0, "errors": []},
    )
    common = dict(
        db_path=scratch / "app.db",
        settings=disabled_settings(),
        log_path=scratch / "visits.jsonl",
    )
    sync.run(force=True, **common)
    second = sync.run(**common)          # inside the interval
    assert second["skipped"] is True
    assert "interval" in second["reason"]
    assert sync.run(force=True, **common)["skipped"] is False


def test_sync_state_is_written_and_readable(scratch, monkeypatch):
    monkeypatch.setattr(
        sync, "sync_local_chrome",
        lambda **kwargs: {"profiles": [], "visits_logged": 0, "errors": []},
    )
    sync.run(
        db_path=scratch / "app.db",
        settings=disabled_settings(),
        log_path=scratch / "visits.jsonl",
        force=True,
    )
    state = sync.load_state()
    assert state["ok"] is True
    assert state["last_success"]


def test_backfill_makes_the_log_a_complete_record(scratch):
    """A DB-only visit becomes durable, and the DB rebuilds from the log.

    This is the guarantee that matters: deleting app.db must not lose
    history, even history captured before the log existed.
    """
    from app import ingest, storage
    from fakes import FakeResponse, FakeSession, api_payload

    log_path = scratch / "visits.jsonl"
    conn = storage.connect(scratch / "app.db")
    try:
        # A visit ingested the old way: in the DB, with no provenance.
        ingest.ingest_document(
            {
                "schema_version": "1.0",
                "source_browser": "chrome",
                "exported_at": "2026-01-01T00:00:00Z",
                "visits": [{"url": CAT_URL, "title": "Cat",
                            "timestamp": "2026-01-01T10:00:00Z"}],
            },
            conn,
            session=FakeSession([FakeResponse(api_payload(pages=["Cat"]))]),
        )
        assert len(storage.visits_without_provenance(conn)) == 1
        assert visitlog.read(log_path) == []

        filled = sync.backfill_from_db(conn, log_path=log_path)
        assert filled == {"db_only_visits": 1, "new_to_log": 1}

        entries = visitlog.read(log_path)
        assert len(entries) == 1
        assert entries[0]["source"] == "legacy_db"
        assert entries[0]["url"] == CAT_URL

        # Running it twice must not duplicate.
        assert sync.backfill_from_db(conn, log_path=log_path)["new_to_log"] == 0
    finally:
        conn.close()

    # The real test: a brand-new database rebuilt from the log alone.
    rebuilt = storage.connect(scratch / "rebuilt.db")
    try:
        report = ingest.ingest_document(
            visitlog.to_document(visitlog.read(log_path)),
            rebuilt,
            session=FakeSession([FakeResponse(api_payload(pages=["Cat"]))]),
        )
        assert report["visits_total"] == 1
        assert report["pages_total"] == 1
    finally:
        rebuilt.close()


def test_backfill_reconstructs_awkward_titles(scratch):
    """Titles with slashes and spaces must round-trip through the URL form."""
    from app import ingest, normalize, storage
    from fakes import FakeResponse, FakeSession, api_payload

    log_path = scratch / "visits.jsonl"
    conn = storage.connect(scratch / "app.db")
    try:
        ingest.ingest_document(
            {
                "schema_version": "1.0",
                "source_browser": "chrome",
                "exported_at": "2026-01-01T00:00:00Z",
                "visits": [
                    {"url": "https://en.wikipedia.org/wiki/OS/2",
                     "title": "", "timestamp": "2026-01-01T10:00:00Z"},
                    {"url": "https://en.wikipedia.org/wiki/Douglas_Adams",
                     "title": "", "timestamp": "2026-01-01T11:00:00Z"},
                ],
            },
            conn,
            session=FakeSession([
                FakeResponse(api_payload(pages=["OS/2", "Douglas Adams"])),
            ]),
        )
        sync.backfill_from_db(conn, log_path=log_path)
    finally:
        conn.close()

    titles = {normalize.url_to_title(e["url"]) for e in visitlog.read(log_path)}
    assert titles == {"OS/2", "Douglas Adams"}


def fake_chrome_dir(root):
    """A Chrome user-data directory with three profiles and a Local State."""
    (root / "Default").mkdir(parents=True)
    (root / "Profile 1").mkdir()
    (root / "Profile 2").mkdir()
    (root / "System Profile").mkdir()
    for name in ("Default", "Profile 1", "Profile 2", "System Profile"):
        (root / name / "History").write_bytes(b"")
    (root / "Local State").write_text(json.dumps({
        "profile": {"info_cache": {
            "Default": {"name": "Person 1", "user_name": ""},
            "Profile 1": {"name": "Work", "user_name": "work@example.com"},
            "Profile 2": {"name": "Joseph", "user_name": "joe@example.com"},
        }}
    }), encoding="utf-8")
    return root


def test_profiles_carry_display_names_and_accounts(tmp_path, monkeypatch):
    root = fake_chrome_dir(tmp_path / "Chrome")
    monkeypatch.setattr(eh, "chrome_base_dir", lambda: root)
    found = eh.find_chrome_profiles()

    # System Profile never holds browsing and is excluded.
    assert [p["directory"] for p in found] == ["Default", "Profile 1", "Profile 2"]
    assert found[1]["display_name"] == "Work"
    assert found[1]["account"] == "work@example.com"


def test_profile_filter_matches_directory_or_display_name(tmp_path, monkeypatch):
    root = fake_chrome_dir(tmp_path / "Chrome")
    monkeypatch.setattr(eh, "chrome_base_dir", lambda: root)

    by_directory = eh.find_chrome_profiles(only=["Profile 2"])
    by_display = eh.find_chrome_profiles(only=["Joseph"])
    case_insensitive = eh.find_chrome_profiles(only=["joseph"])
    assert [p["directory"] for p in by_directory] == ["Profile 2"]
    assert [p["directory"] for p in by_display] == ["Profile 2"]
    assert [p["directory"] for p in case_insensitive] == ["Profile 2"]

    both = eh.find_chrome_profiles(only=["Work", "Joseph"])
    assert [p["directory"] for p in both] == ["Profile 1", "Profile 2"]


def test_empty_filter_means_all_profiles(tmp_path, monkeypatch):
    """The default: reading one profile would silently capture nothing."""
    root = fake_chrome_dir(tmp_path / "Chrome")
    monkeypatch.setattr(eh, "chrome_base_dir", lambda: root)
    assert len(eh.find_chrome_profiles(only=[])) == 3
    assert len(eh.find_chrome_profiles(only=None)) == 3


def test_missing_local_state_falls_back_to_directory_names(tmp_path, monkeypatch):
    root = fake_chrome_dir(tmp_path / "Chrome")
    (root / "Local State").unlink()
    monkeypatch.setattr(eh, "chrome_base_dir", lambda: root)
    found = eh.find_chrome_profiles()
    assert found[1]["display_name"] == "Profile 1"
    assert found[1]["account"] == ""


def test_unmatched_profile_filter_reports_what_is_available(tmp_path, monkeypatch):
    root = fake_chrome_dir(tmp_path / "Chrome")
    monkeypatch.setattr(eh, "chrome_base_dir", lambda: root)
    report = sync.sync_local_chrome(
        log_path=tmp_path / "visits.jsonl", only=["Nonexistent"]
    )
    assert report["visits_logged"] == 0
    message = " ".join(report["errors"])
    assert "Nonexistent" in message and "Work" in message


def test_config_profile_selection_roundtrips(tmp_path):
    path = tmp_path / "config.local.toml"
    settings = localconfig.load(path)
    assert localconfig.chrome_profiles(settings) == []
    settings["local"]["chrome_profiles"] = ["Profile 1", "Profile 2"]
    localconfig.save(settings, path)
    assert localconfig.chrome_profiles(localconfig.load(path)) == [
        "Profile 1", "Profile 2"
    ]


def test_config_tolerates_a_bare_string_profile(tmp_path):
    settings = localconfig.load(tmp_path / "absent.toml")
    settings["local"]["chrome_profiles"] = "Profile 1"   # hand-edited
    assert localconfig.chrome_profiles(settings) == ["Profile 1"]


def test_archive_processed_once_then_skipped(scratch):
    from app import storage

    conn = storage.connect(scratch / "app.db")
    try:
        assert not storage.is_archive_processed(conn, "file1", "2026-01-01T00:00:00Z")
        storage.record_processed_archive(
            conn, "file1", "2026-01-01T00:00:00Z", "a@example.com", "t.zip", 42
        )
        conn.commit()
        assert storage.is_archive_processed(conn, "file1", "2026-01-01T00:00:00Z")
        # A re-uploaded archive keeps its ID but gets a new modified time,
        # and does need reprocessing.
        assert not storage.is_archive_processed(conn, "file1", "2026-02-01T00:00:00Z")
        assert storage.processed_archives(conn)[0]["visits_extracted"] == 42
    finally:
        conn.close()
