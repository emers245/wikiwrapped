"""Cross-source deduplication (D11) and visit provenance (D10).

The four cases that matter, each tested at the unit level (app.dedup.collapse)
and end to end through app.ingest into the database:

1. the same visit reported by both sources          -> ONE visit row
2. the same URL at genuinely different times        -> TWO visit rows
3. timestamps within the configured tolerance       -> ONE visit row
4. URL variants normalizing to the same article     -> ONE visit row

Case 3 is the one that would silently inflate every statistic if the two
export mechanisms disagreed and the tolerance were too tight, so it is
tested against an explicit tolerance rather than only the default of 0.
"""

import pytest

from app import dedup, ingest, storage, visitlog
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


def visit(url, stamp, source=None, account=None):
    row = {"url": url, "title": "irrelevant", "timestamp": stamp}
    if source:
        row["source"] = source
    if account:
        row["account"] = account
    return row


def entry(key, stamp, source="takeout", account=None):
    return {"key": key, "timestamp": stamp, "source": source, "account": account}


# ---------------------------------------------------------------------------
# Unit level: app.dedup.collapse
# ---------------------------------------------------------------------------

def test_same_visit_from_both_sources_collapses_with_merged_provenance():
    collapsed = dedup.collapse([
        entry("Cat", "2026-01-01T10:00:00Z", "chrome_local", None),
        entry("Cat", "2026-01-01T10:00:00Z", "takeout", "a@example.com"),
    ])
    assert len(collapsed) == 1
    assert collapsed[0]["record_count"] == 2
    assert collapsed[0]["sources"] == [
        {"source": "chrome_local", "account": None},
        {"source": "takeout", "account": "a@example.com"},
    ]


def test_genuinely_different_times_stay_separate():
    collapsed = dedup.collapse([
        entry("Cat", "2026-01-01T10:00:00Z"),
        entry("Cat", "2026-01-01T10:05:00Z"),
    ])
    assert len(collapsed) == 2


def test_default_tolerance_does_not_merge_adjacent_seconds():
    """The shipped default is exact-second matching."""
    assert dedup.DEDUP_TOLERANCE_S == 0
    collapsed = dedup.collapse([
        entry("Cat", "2026-01-01T10:00:00Z", "chrome_local"),
        entry("Cat", "2026-01-01T10:00:01Z", "takeout"),
    ])
    assert len(collapsed) == 2


def test_tolerance_merges_near_identical_timestamps():
    """With slack configured, a one-second disagreement is one visit."""
    collapsed = dedup.collapse(
        [
            entry("Cat", "2026-01-01T10:00:00Z", "chrome_local"),
            entry("Cat", "2026-01-01T10:00:01Z", "takeout", "a@example.com"),
        ],
        tolerance_s=2,
    )
    assert len(collapsed) == 1
    # The earliest timestamp represents the cluster, so the result does not
    # depend on which source happened to be ingested first.
    assert collapsed[0]["timestamp"] == "2026-01-01T10:00:00Z"
    assert len(collapsed[0]["sources"]) == 2


def test_tolerance_compares_against_cluster_start_not_neighbour():
    """A dense run must not chain into one arbitrarily wide cluster."""
    collapsed = dedup.collapse(
        [entry("Cat", f"2026-01-01T10:00:{s:02d}Z") for s in (0, 2, 4, 6)],
        tolerance_s=2,
    )
    # (0,2) then (4,6): four visits become two, not one.
    assert [c["timestamp"] for c in collapsed] == [
        "2026-01-01T10:00:00Z",
        "2026-01-01T10:00:04Z",
    ]


def test_different_pages_at_the_same_instant_are_separate():
    collapsed = dedup.collapse([
        entry("Cat", "2026-01-01T10:00:00Z"),
        entry("Dog", "2026-01-01T10:00:00Z"),
    ])
    assert len(collapsed) == 2


def test_absurd_tolerance_is_refused():
    with pytest.raises(ValueError, match="sanity cap"):
        dedup.collapse([entry("Cat", "2026-01-01T10:00:00Z")], tolerance_s=3600)


# ---------------------------------------------------------------------------
# End to end through ingest into the database
# ---------------------------------------------------------------------------

def test_identical_visit_from_both_sources_yields_one_row(conn):
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    report = ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z",
              "chrome_local"),
        visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z",
              "takeout", "a@example.com"),
    ]), conn, session=session)

    assert report["visits_total"] == 1
    assert report["visits_deduplicated"] == 1
    # One visit, but both mechanisms recorded against it.
    assert report["visit_source_rows"] == 2
    assert storage.count_multi_source_visits(conn) == 1
    assert storage.visit_source_summary(conn) == [
        {"source": "chrome_local", "account": "", "visits": 1},
        {"source": "takeout", "account": "a@example.com", "visits": 1},
    ]


def test_same_page_different_times_yields_two_rows(conn):
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    report = ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z",
              "chrome_local"),
        visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T11:30:00Z",
              "takeout", "a@example.com"),
    ]), conn, session=session)

    assert report["visits_total"] == 2
    assert report["visits_deduplicated"] == 0
    assert storage.count_multi_source_visits(conn) == 0


def test_url_variants_normalizing_to_one_page_collapse(conn):
    """Mobile host, a redirect, and a query string are all the same article."""
    session = FakeSession([
        FakeResponse(api_payload(
            redirects=[("USA", "United States")],
            pages=["United States"],
        )),
    ])
    report = ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/USA", "2026-01-01T10:00:00Z",
              "takeout", "a@example.com"),
        visit("https://en.m.wikipedia.org/wiki/United_States",
              "2026-01-01T10:00:00Z", "chrome_local"),
        visit("https://en.wikipedia.org/wiki/United_States?action=raw",
              "2026-01-01T10:00:00Z", "takeout", "b@example.com"),
    ]), conn, session=session)

    assert report["pages_total"] == 1
    assert report["visits_total"] == 1
    assert report["visits_deduplicated"] == 2
    # Three provenance rows on the single surviving visit.
    assert report["visit_source_rows"] == 3
    assert storage.count_multi_source_visits(conn) == 1


def test_second_source_arriving_later_attaches_to_existing_visit(conn):
    """The realistic sequence: local Chrome this week, Takeout next month."""
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z",
              "chrome_local"),
    ]), conn, session=session)

    report = ingest.ingest_document(doc([
        visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z",
              "takeout", "a@example.com"),
    ]), conn, session=FakeSession([]))

    assert report["visits_inserted"] == 0      # no duplicate visit
    assert report["visit_source_rows"] == 1    # but provenance recorded
    assert storage.count_visits(conn) == 1
    assert storage.count_multi_source_visits(conn) == 1


def test_provenance_is_idempotent_across_reingest(conn):
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    rows = [visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z",
                  "takeout", "a@example.com")]
    ingest.ingest_document(doc(rows), conn, session=session)
    report = ingest.ingest_document(doc(rows), conn, session=FakeSession([]))

    assert report["visit_source_rows"] == 0
    assert storage.visit_source_summary(conn) == [
        {"source": "takeout", "account": "a@example.com", "visits": 1},
    ]


def test_defaults_apply_to_rows_without_provenance(conn):
    """A plain schema-1.0 export still ingests, tagged by the caller."""
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    report = ingest.ingest_document(
        doc([visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z")]),
        conn,
        session=session,
        default_source=visitlog.SOURCE_CHROME_LOCAL,
    )
    assert report["visit_source_rows"] == 1
    assert storage.visit_source_summary(conn) == [
        {"source": "chrome_local", "account": "", "visits": 1},
    ]


def test_compact_visit_sources_drops_superseded_rows(conn):
    """Same visit and source, recorded once without an account and once with."""
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    url, stamp = "https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z"
    ingest.ingest_document(
        doc([visit(url, stamp, "chrome_local")]), conn, session=session
    )
    ingest.ingest_document(
        doc([visit(url, stamp, "chrome_local", "work@example.com")]),
        conn, session=FakeSession([]),
    )
    assert len(storage.visit_source_summary(conn)) == 2

    assert storage.compact_visit_sources(conn) == 1
    assert storage.visit_source_summary(conn) == [
        {"source": "chrome_local", "account": "work@example.com", "visits": 1},
    ]


def test_compact_visit_sources_keeps_distinct_accounts(conn):
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    url, stamp = "https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z"
    ingest.ingest_document(doc([
        visit(url, stamp, "chrome_local", "a@example.com"),
        visit(url, stamp, "takeout", "b@example.com"),
    ]), conn, session=session)

    assert storage.compact_visit_sources(conn) == 0
    assert storage.count_multi_source_visits(conn) == 1


def test_untagged_export_records_no_provenance(conn):
    """Backward compatibility: no source field, no visit_sources rows."""
    session = FakeSession([FakeResponse(api_payload(pages=["Cat"]))])
    report = ingest.ingest_document(
        doc([visit("https://en.wikipedia.org/wiki/Cat", "2026-01-01T10:00:00Z")]),
        conn,
        session=session,
    )
    assert report["visits_total"] == 1
    assert report["visit_source_rows"] == 0
    assert storage.visit_source_summary(conn) == []
