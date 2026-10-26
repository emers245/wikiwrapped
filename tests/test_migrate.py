"""app.migrate: copy-only migration of repo-local user data.

Every rule from the module docstring has a test here, because the file it
exists to protect (the visit log) cannot be recreated if it is lost."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from app import migrate, storage, visitlog


def make_legacy_repo(root):
    """A fake repo checkout holding every kind of legacy user data."""
    (root / "data" / "reference").mkdir(parents=True)
    (root / "data" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "data" / "reference" / "umap_reducer.pkl").write_bytes(b"resource")
    (root / "data" / "sync_state.json").write_text('{"ok": true}', encoding="utf-8")
    visitlog.append(
        [
            visitlog.record("https://en.wikipedia.org/wiki/Cat", "Cat",
                            "2025-01-01T00:00:00Z", visitlog.SOURCE_CHROME_LOCAL),
            visitlog.record("https://en.wikipedia.org/wiki/Dog", "Dog",
                            "2025-01-02T00:00:00Z", visitlog.SOURCE_TAKEOUT,
                            account="me@example.com"),
        ],
        path=root / "data" / "visits.jsonl",
    )
    conn = storage.connect(root / "data" / "app.db")
    storage.upsert_topic(conn, "Cat", "STEM.Biology", "STEM.Biology")
    conn.commit()
    conn.close()
    (root / "config.local.toml").write_text("[local]\nchrome_profiles = []\n",
                                            encoding="utf-8")
    (root / "tokens").mkdir()
    token = root / "tokens" / "me@example.com.json"
    token.write_text('{"token": "x"}', encoding="utf-8")
    token.chmod(0o600)
    (root / "client_secret_123.apps.json").write_text('{"installed": {}}',
                                                      encoding="utf-8")
    return root


def snapshot(root):
    """Hash of every file under root, to prove sources are untouched."""
    return {
        str(path.relative_to(root)): migrate.file_sha256(path)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


@pytest.fixture
def repo(tmp_path):
    return make_legacy_repo(tmp_path / "repo")


def by_path(report):
    """Items by relative path, written with / on every platform."""
    return {item.relpath.as_posix(): item for item in report["items"]}


def test_copies_every_kind_of_user_data_and_verifies(repo, tmp_path):
    dest = tmp_path / "userdata"
    report = migrate.migrate(repo, dest)
    items = by_path(report)

    assert report["ok"] and report["sources_unchanged"]
    assert set(items) == {
        "data/app.db", "data/sync_state.json", "data/visits.jsonl",
        "config.local.toml", "tokens/me@example.com.json",
        "client_secret_123.apps.json",
    }
    assert all(item.action == "copy" and item.verified for item in items.values())
    assert visitlog.read(dest / "data" / "visits.jsonl") == visitlog.read(
        repo / "data" / "visits.jsonl"
    )
    conn = storage.connect(dest / "data" / "app.db")
    assert storage.get_topic(conn, "Cat")["macro_topic"] == "STEM.Biology"
    conn.close()


def test_reference_space_and_gitkeep_are_not_user_data(repo, tmp_path):
    dest = tmp_path / "userdata"
    migrate.migrate(repo, dest)
    assert not (dest / "data" / "reference").exists()
    assert not (dest / "data" / ".gitkeep").exists()


def test_sources_are_never_modified_or_removed(repo, tmp_path):
    before = snapshot(repo)
    migrate.migrate(repo, tmp_path / "userdata")
    assert snapshot(repo) == before


# Windows has no owner-only permission bits (chmod there only sets read-only);
# the user's own profile folder is what keeps the token private.
@pytest.mark.skipif(os.name == "nt", reason="no POSIX permission bits on Windows")
def test_token_permissions_survive_the_copy(repo, tmp_path):
    dest = tmp_path / "userdata"
    migrate.migrate(repo, dest)
    mode = (dest / "tokens" / "me@example.com.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_dry_run_writes_nothing(repo, tmp_path):
    dest = tmp_path / "userdata"
    report = migrate.migrate(repo, dest, dry_run=True)
    assert not dest.exists()
    assert {item.action for item in report["items"]} == {"copy"}


def test_rerun_is_idempotent(repo, tmp_path):
    dest = tmp_path / "userdata"
    migrate.migrate(repo, dest)
    second = migrate.migrate(repo, dest)
    assert second["ok"]
    assert {item.action for item in second["items"]} == {"identical"}


def test_a_differing_destination_is_refused_not_overwritten(repo, tmp_path):
    dest = tmp_path / "userdata"
    dest.mkdir()
    (dest / "config.local.toml").write_text("# newer settings\n", encoding="utf-8")

    report = migrate.migrate(repo, dest)
    item = by_path(report)["config.local.toml"]

    assert item.action == "conflict"
    assert not report["ok"]
    assert (dest / "config.local.toml").read_text(encoding="utf-8") == "# newer settings\n"


def test_a_differing_destination_database_is_refused(repo, tmp_path):
    dest = tmp_path / "userdata"
    conn = storage.connect(dest / "data" / "app.db")
    storage.upsert_topic(conn, "Other", None, None)
    conn.commit()
    conn.close()

    report = migrate.migrate(repo, dest)
    assert by_path(report)["data/app.db"].action == "conflict"
    conn = storage.connect(dest / "data" / "app.db")
    assert storage.get_topic(conn, "Cat") is None   # untouched
    conn.close()


def test_two_differing_logs_are_merged_without_losing_either(repo, tmp_path):
    """The app ran against the new location before migrating: both logs
    hold records the other lacks, and the union must survive."""
    dest = tmp_path / "userdata"
    newer = visitlog.record("https://en.wikipedia.org/wiki/Fox", "Fox",
                            "2026-01-01T00:00:00Z", visitlog.SOURCE_CHROME_LOCAL)
    visitlog.append([newer], path=dest / "data" / "visits.jsonl")

    report = migrate.migrate(repo, dest)
    item = by_path(report)["data/visits.jsonl"]

    assert item.action == "merge" and item.verified
    merged = {visitlog.dedup_key(r) for r in visitlog.read(dest / "data" / "visits.jsonl")}
    source = {visitlog.dedup_key(r) for r in visitlog.read(repo / "data" / "visits.jsonl")}
    assert source <= merged and visitlog.dedup_key(newer) in merged
    assert len(merged) == len(source) + 1

    # Merging again adds nothing: the merge is a union, not a concatenation.
    again = by_path(migrate.migrate(repo, dest))["data/visits.jsonl"]
    assert again.action in ("merge", "identical")
    assert len(visitlog.read(dest / "data" / "visits.jsonl")) == len(merged)


def test_database_copy_is_consistent_with_a_live_writer(repo, tmp_path):
    """An open connection with uncommitted work must not leak into the copy."""
    conn = sqlite3.connect(str(repo / "data" / "app.db"))
    conn.execute("BEGIN")
    conn.execute("INSERT INTO topic_cache VALUES ('Uncommitted', NULL, NULL, 'x')")
    try:
        dest = tmp_path / "userdata"
        assert migrate.migrate(repo, dest)["ok"]
    finally:
        conn.rollback()
        conn.close()
    copy = storage.connect(dest / "data" / "app.db")
    assert storage.get_topic(copy, "Uncommitted") is None
    copy.close()


def test_same_source_and_destination_is_refused(repo):
    with pytest.raises(ValueError):
        migrate.migrate(repo, repo)


def test_nothing_to_migrate_is_fine(tmp_path):
    (tmp_path / "empty").mkdir()
    report = migrate.migrate(tmp_path / "empty", tmp_path / "userdata")
    assert report["items"] == [] and report["ok"]


# ---------------------------------------------------------------------------
# The "not migrated yet" warning
# ---------------------------------------------------------------------------

def test_unmigrated_log_is_reported(repo, tmp_path):
    status = migrate.legacy_status(repo, tmp_path / "userdata")
    assert status and "python -m app.migrate" in status["message"]


def test_migrated_log_is_quiet_until_it_changes(repo, tmp_path):
    dest = tmp_path / "userdata"
    migrate.migrate(repo, dest)
    assert migrate.legacy_status(repo, dest) is None

    # Old code ran against the repo again: the warning must come back.
    visitlog.append(
        [visitlog.record("https://en.wikipedia.org/wiki/Owl", "Owl",
                         "2026-02-01T00:00:00Z", visitlog.SOURCE_CHROME_LOCAL)],
        path=repo / "data" / "visits.jsonl",
    )
    assert migrate.legacy_status(repo, dest) is not None


def test_no_legacy_log_means_no_warning(tmp_path):
    (tmp_path / "repo").mkdir()
    assert migrate.legacy_status(tmp_path / "repo", tmp_path / "userdata") is None


def test_cli_dry_run_reports_and_exits_zero(repo, tmp_path, capsys):
    code = migrate.main(["--dry-run", "--source", str(repo),
                         "--dest", str(tmp_path / "userdata")])
    out = capsys.readouterr().out
    assert code == 0
    # The report uses the platform's own separator (data\\visits.jsonl on Windows).
    assert "DRY RUN" in out and str(Path("data", "visits.jsonl")) in out
    assert not (tmp_path / "userdata").exists()


def test_marker_records_what_was_migrated(repo, tmp_path):
    dest = tmp_path / "userdata"
    migrate.migrate(repo, dest)
    marker = json.loads((dest / "data" / migrate.MARKER_NAME).read_text())
    assert marker["source"] == str(repo.resolve())
    assert marker["log_size"] == (repo / "data" / "visits.jsonl").stat().st_size
