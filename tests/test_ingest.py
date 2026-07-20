"""Tests for app.storage + app.ingest (repo_spec S6, D2, D5, D6).

All offline: redirect resolution goes through FakeSession. The key
behaviors under test are redirect merging into one page, idempotent
re-ingest, incremental updates, the persistent redirect cache preventing
repeat lookups, and visible (counted) drops.
"""

import pytest

from app import ingest, storage
from fakes import FakeResponse, FakeSession, api_payload


@pytest.fixture
def conn():
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


def doc(visits):
    return {
        "schema_version": "1.0",
        "source_browser": "chrome",
        "exported_at": "2026-01-01T00:00:00Z",
        "visits": visits,
    }


def visit(url, stamp):
    return {"url": url, "title": "irrelevant", "timestamp": stamp}


FOUR_VISITS = [
    visit("https://en.wikipedia.org/wiki/Douglas_Adams", "2025-01-01T10:00:00Z"),
    visit("https://en.wikipedia.org/wiki/USA", "2025-01-01T11:00:00Z"),
    visit("https://en.m.wikipedia.org/wiki/United_States", "2025-01-02T09:00:00Z"),
    visit("https://en.wikipedia.org/wiki/File:Example.jpg", "2025-01-01T12:00:00Z"),
]


def usa_session():
    return FakeSession([
        FakeResponse(api_payload(
            redirects=[("USA", "United States")],
            pages=["United States", "Douglas Adams"],
        )),
    ])


def test_rejects_wrong_schema_version(conn):
    with pytest.raises(ValueError, match="schema_version"):
        ingest.ingest_document({"schema_version": "0.9", "visits": []}, conn)


def test_ingest_merges_redirects_into_one_page(conn):
    report = ingest.ingest_document(doc(FOUR_VISITS), conn, session=usa_session())

    assert report["pages_new"] == 2
    assert report["visits_inserted"] == 3
    assert report["visits_dropped_non_article"] == 1
    assert report["dropped_url_samples"] == [
        "https://en.wikipedia.org/wiki/File:Example.jpg"
    ]

    pages = {
        row["canonical_title"]: row
        for row in conn.execute("SELECT * FROM pages")
    }
    # Desktop redirect (USA) and mobile direct URL are the same page (D2, D5)
    assert pages["United States"]["visit_count"] == 2
    assert pages["United States"]["first_seen"] == "2025-01-01T11:00:00Z"
    assert pages["United States"]["raw_title"] == "USA"  # earliest visit's raw title
    assert pages["Douglas Adams"]["visit_count"] == 1


def test_reingest_is_idempotent_and_uses_cache(conn):
    ingest.ingest_document(doc(FOUR_VISITS), conn, session=usa_session())

    # Empty queue: any network request would raise IndexError
    silent = FakeSession([])
    report = ingest.ingest_document(doc(FOUR_VISITS), conn, session=silent)

    assert silent.calls == []
    assert report["visits_inserted"] == 0
    assert report["visits_already_present"] == 3
    assert report["pages_new"] == 0
    assert report["visits_total"] == 3


def test_incremental_update_adds_only_new_visits(conn):
    ingest.ingest_document(doc(FOUR_VISITS), conn, session=usa_session())

    updated = FOUR_VISITS + [
        visit("https://en.wikipedia.org/wiki/Douglas_Adams", "2025-02-01T08:00:00Z"),
    ]
    report = ingest.ingest_document(doc(updated), conn, session=FakeSession([]))

    assert report["visits_inserted"] == 1
    assert report["visits_total"] == 4
    row = conn.execute(
        "SELECT visit_count, first_seen FROM pages WHERE canonical_title = ?",
        ("Douglas Adams",),
    ).fetchone()
    assert row["visit_count"] == 2
    assert row["first_seen"] == "2025-01-01T10:00:00Z"  # unchanged by later visit


def test_unresolvable_title_cached_as_none_and_not_requeried(conn):
    session = FakeSession([
        FakeResponse(api_payload(missing=["No Such Page Xyz"])),
    ])
    report = ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/No_Such_Page_Xyz", "2025-01-01T10:00:00Z"),
    ]), conn, session=session)
    assert report["visits_dropped_non_article"] == 1
    assert report["visits_total"] == 0

    cached = storage.get_redirect_cache(conn)
    assert cached == {"No Such Page Xyz": None}

    # Second ingest: the negative result is served from cache, no network
    silent = FakeSession([])
    ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/No_Such_Page_Xyz", "2025-01-02T10:00:00Z"),
    ]), conn, session=silent)
    assert silent.calls == []


def test_malformed_rows_counted_not_fatal(conn):
    report = ingest.ingest_document(doc([
        {"title": "no url", "timestamp": "2025-01-01T10:00:00Z"},
        {"url": "https://en.wikipedia.org/wiki/Cat"},  # no timestamp
        "not even a dict",
    ]), conn, session=FakeSession([]))
    assert report["malformed_rows"] == 3
    assert report["visits_total"] == 0


def test_same_second_desktop_and_mobile_collapse(conn):
    # Same canonical page, same second, two raw URLs -> one visit row
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    report = ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/Cat", "2025-01-01T10:00:00Z"),
        visit("https://en.m.wikipedia.org/wiki/Cat", "2025-01-01T10:00:00Z"),
    ]), conn, session=session)
    assert report["visits_inserted"] == 1
    assert report["visits_already_present"] == 1


def test_storage_schema_has_all_cache_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "pages", "visits", "redirect_cache", "topic_cache",
        "pageview_cache", "embedding_cache", "umap_coords",
    } <= tables
