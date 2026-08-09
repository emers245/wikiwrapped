"""The append-only visit log (D9): the durable source of truth.

app.db can be deleted and rebuilt from this file; this file cannot be
rebuilt from anything, because Chrome expires local history after about 90
days. So the properties under test are: appends never lose data, the file is
never rewritten by the sync path, a torn line cannot make the log
unreadable, and re-logging the same record is a no-op.
"""

import json

import pytest

from app import visitlog


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "visits.jsonl"


def rec(url, stamp, source="chrome_local", account=None):
    return visitlog.record(url, "Title", stamp, source, account)


CAT = "https://en.wikipedia.org/wiki/Cat"
DOG = "https://en.wikipedia.org/wiki/Dog"


def test_append_then_read_roundtrip(log_path):
    written = visitlog.append(
        [rec(CAT, "2026-01-01T10:00:00Z"), rec(DOG, "2026-01-01T11:00:00Z")],
        path=log_path,
    )
    assert written == 2
    entries = visitlog.read(log_path)
    assert [e["url"] for e in entries] == [CAT, DOG]
    assert entries[0]["source"] == "chrome_local"
    assert entries[0]["account"] is None


def test_append_is_idempotent_for_identical_records(log_path):
    record = rec(CAT, "2026-01-01T10:00:00Z")
    assert visitlog.append([record], path=log_path) == 1
    assert visitlog.append([record], path=log_path) == 0
    assert len(visitlog.read(log_path)) == 1


def test_same_visit_from_two_sources_is_kept_twice(log_path):
    """Both records survive here on purpose.

    They are the evidence that two mechanisms saw one event, which is what
    visit_sources records. Collapsing them is app.dedup's job, at DB build
    time, where canonical post-redirect titles are available.
    """
    visitlog.append([
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", None),
        rec(CAT, "2026-01-01T10:00:00Z", "takeout", "a@example.com"),
    ], path=log_path)
    entries = visitlog.read(log_path)
    assert len(entries) == 2
    assert {e["source"] for e in entries} == {"chrome_local", "takeout"}


def test_append_only_never_rewrites_existing_lines(log_path):
    visitlog.append([rec(CAT, "2026-01-01T10:00:00Z")], path=log_path)
    first = log_path.read_text(encoding="utf-8")
    visitlog.append([rec(DOG, "2026-01-02T10:00:00Z")], path=log_path)
    assert log_path.read_text(encoding="utf-8").startswith(first)


def test_torn_final_line_is_skipped_not_fatal(log_path):
    """A sync killed mid-write must not make the whole log unreadable."""
    visitlog.append([rec(CAT, "2026-01-01T10:00:00Z")], path=log_path)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write('{"url": "https://en.wikipedia.org/wiki/Tr')
    entries = visitlog.read(log_path)
    assert len(entries) == 1
    assert entries[0]["url"] == CAT


def test_records_without_url_or_timestamp_are_ignored(log_path):
    log_path.write_text(
        json.dumps({"url": CAT}) + "\n"
        + json.dumps({"timestamp": "2026-01-01T10:00:00Z"}) + "\n"
        + json.dumps({"url": DOG, "timestamp": "2026-01-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    assert [e["url"] for e in visitlog.read(log_path)] == [DOG]


def test_known_set_is_shared_across_batches(log_path):
    """One sync pass appends several batches without re-reading the file."""
    known = visitlog.existing_keys(log_path)
    record = rec(CAT, "2026-01-01T10:00:00Z")
    assert visitlog.append([record], path=log_path, known=known) == 1
    assert visitlog.append([record], path=log_path, known=known) == 0


def test_latest_timestamp_scoped_by_source(log_path):
    visitlog.append([
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local"),
        rec(DOG, "2026-03-01T10:00:00Z", "takeout", "a@example.com"),
    ], path=log_path)
    assert visitlog.latest_timestamp(path=log_path) == "2026-03-01T10:00:00Z"
    assert visitlog.latest_timestamp(
        source="chrome_local", path=log_path
    ) == "2026-01-01T10:00:00Z"


def test_stats_counts_by_source_and_account(log_path):
    visitlog.append([
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local"),
        rec(DOG, "2026-01-02T10:00:00Z", "takeout", "a@example.com"),
        rec(CAT, "2026-01-03T10:00:00Z", "takeout", "b@example.com"),
    ], path=log_path)
    stats = visitlog.stats(log_path)
    assert stats["records"] == 3
    assert stats["by_source"] == {"chrome_local": 1, "takeout": 2}
    assert stats["accounts"] == ["a@example.com", "b@example.com"]
    assert stats["earliest"] == "2026-01-01T10:00:00Z"
    assert stats["latest"] == "2026-01-03T10:00:00Z"


def test_missing_log_reads_as_empty(tmp_path):
    assert visitlog.read(tmp_path / "absent.jsonl") == []
    assert visitlog.stats(tmp_path / "absent.jsonl")["records"] == 0


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="Unknown source"):
        visitlog.record(CAT, "T", "2026-01-01T10:00:00Z", "carrier_pigeon")


def test_record_requires_url_and_timestamp():
    with pytest.raises(ValueError):
        visitlog.record("", "T", "2026-01-01T10:00:00Z", "takeout")
    with pytest.raises(ValueError):
        visitlog.record(CAT, "T", "", "takeout")


def test_to_document_is_the_ingest_schema_sorted_by_time(log_path):
    visitlog.append([
        rec(DOG, "2026-01-02T10:00:00Z", "takeout", "a@example.com"),
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local"),
    ], path=log_path)
    document = visitlog.to_document(visitlog.read(log_path))
    assert document["schema_version"] == "1.0"
    assert [v["url"] for v in document["visits"]] == [CAT, DOG]
    assert document["visits"][1]["account"] == "a@example.com"


def test_compact_drops_account_less_duplicate(log_path):
    """A visit logged before and after it gained an account is one record."""
    visitlog.append([
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", None),
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", "work@example.com"),
    ], path=log_path)
    assert len(visitlog.read(log_path)) == 2

    result = visitlog.compact(log_path)
    assert result == {"before": 2, "after": 1, "removed": 1}
    entries = visitlog.read(log_path)
    assert len(entries) == 1
    assert entries[0]["account"] == "work@example.com"


def test_compact_keeps_two_genuinely_different_accounts(log_path):
    visitlog.append([
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", "a@example.com"),
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", "b@example.com"),
    ], path=log_path)
    assert visitlog.compact(log_path)["removed"] == 0
    assert len(visitlog.read(log_path)) == 2


def test_compact_keeps_different_sources(log_path):
    visitlog.append([
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", None),
        rec(CAT, "2026-01-01T10:00:00Z", "takeout", "a@example.com"),
    ], path=log_path)
    assert visitlog.compact(log_path)["removed"] == 0
    assert len(visitlog.read(log_path)) == 2


def test_compact_is_idempotent(log_path):
    visitlog.append([
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", None),
        rec(CAT, "2026-01-01T10:00:00Z", "chrome_local", "work@example.com"),
    ], path=log_path)
    visitlog.compact(log_path)
    assert visitlog.compact(log_path)["removed"] == 0


def test_rewrite_replaces_atomically(log_path):
    visitlog.append([rec(CAT, "2026-01-01T10:00:00Z")], path=log_path)
    assert visitlog.rewrite([rec(DOG, "2026-02-01T10:00:00Z")], path=log_path) == 1
    entries = visitlog.read(log_path)
    assert len(entries) == 1 and entries[0]["url"] == DOG
