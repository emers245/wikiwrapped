"""Tests for app.stats (repo_spec S7.3, D2) against fixture DBs with
hand-computed expected values, including a DST-crossing case proving IANA
zone handling (the old code's fixed UTC-6 offset would get these wrong).

Timezones are always passed explicitly so tests never depend on the
machine's device zone.
"""

import pytest

from app import stats, storage


@pytest.fixture
def conn():
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


def add_page(conn, title, timestamps, macro=None, raw=None):
    page_id = storage.upsert_page(conn, title, title, min(timestamps))
    storage.insert_visits(conn, page_id, timestamps)
    storage.refresh_visit_counts(conn)
    if macro is not None:
        conn.execute(  # fixture-only SQL; app code goes through storage
            "INSERT INTO topic_cache (canonical_title, macro_topic, raw_label,"
            " fetched_at) VALUES (?, ?, ?, ?)",
            (title, macro, raw, "2026-01-01T00:00:00Z"),
        )


# ---------------------------------------------------------------------------
# summary / most_visited (D2: pages unique, visits total)
# ---------------------------------------------------------------------------

def test_summary_counts_pages_and_visits_distinctly(conn):
    add_page(conn, "Jainism", [
        "2025-01-01T10:00:00Z", "2025-01-01T11:00:00Z", "2025-01-03T09:00:00Z",
    ])
    add_page(conn, "Cat", ["2025-01-02T08:00:00Z"])
    assert stats.summary(conn) == {
        "unique_pages": 2,
        "total_visits": 4,
        "first_visit": "2025-01-01T10:00:00Z",
        "last_visit": "2025-01-03T09:00:00Z",
    }


def test_summary_empty_db(conn):
    assert stats.summary(conn) == {
        "unique_pages": 0,
        "total_visits": 0,
        "first_visit": None,
        "last_visit": None,
    }


def test_most_visited_orders_and_limits(conn):
    add_page(conn, "Jainism", ["2025-01-01T10:00:00Z", "2025-01-01T11:00:00Z"])
    add_page(conn, "Cat", ["2025-01-02T08:00:00Z"])
    add_page(conn, "Ant", ["2025-01-02T09:00:00Z"])  # ties with Cat
    top = stats.most_visited(conn, n=2)
    assert [(p["canonical_title"], p["visit_count"]) for p in top] == [
        ("Jainism", 2), ("Ant", 1),  # tie broken alphabetically
    ]


# ---------------------------------------------------------------------------
# weekday_hour_grid: timezone and DST correctness
# ---------------------------------------------------------------------------

def test_grid_converts_to_local_weekday_and_hour(conn):
    # 03:00 UTC on Wed 2025-01-01 is 21:00 Tue 2024-12-31 in Chicago (CST)
    add_page(conn, "Cat", ["2025-01-01T03:00:00Z"])
    grid = stats.weekday_hour_grid(conn, tz="America/Chicago")
    assert grid["timezone"] == "America/Chicago"
    assert grid["counts"][stats.DAY_NAMES.index("Tuesday")][21] == 1
    assert sum(sum(row) for row in grid["counts"]) == 1


def test_grid_handles_dst_spring_forward(conn):
    # US DST began 2025-03-09: 02:00 CST jumps to 03:00 CDT.
    # 07:30 UTC -> 01:30 CST (UTC-6); 08:30 UTC -> 03:30 CDT (UTC-5).
    # One UTC hour apart, two local hours apart; local 02:xx never exists.
    add_page(conn, "Cat", ["2025-03-09T07:30:00Z", "2025-03-09T08:30:00Z"])
    grid = stats.weekday_hour_grid(conn, tz="America/Chicago")
    sunday = grid["counts"][stats.DAY_NAMES.index("Sunday")]
    assert sunday[1] == 1
    assert sunday[2] == 0
    assert sunday[3] == 1
    # A fixed offset (the old UTC-6 hardcode) would put the second visit
    # in the nonexistent 02:xx hour instead.


def test_grid_respects_tz_argument(conn):
    add_page(conn, "Cat", ["2025-03-09T07:30:00Z", "2025-03-09T08:30:00Z"])
    grid = stats.weekday_hour_grid(conn, tz="UTC")
    sunday = grid["counts"][stats.DAY_NAMES.index("Sunday")]
    assert sunday[7] == 1 and sunday[8] == 1


# ---------------------------------------------------------------------------
# daily_series: zero-fill + trailing rolling average
# ---------------------------------------------------------------------------

def test_daily_series_zero_fills_and_rolls(conn):
    add_page(conn, "Cat", [
        "2025-01-01T10:00:00Z",
        "2025-01-01T12:00:00Z",
        "2025-01-04T10:00:00Z",
    ])
    series = stats.daily_series(conn, tz="UTC", rolling_window=2)
    assert [d["visits"] for d in series["days"]] == [2, 0, 0, 1]
    assert [d["date"] for d in series["days"]] == [
        "2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04",
    ]
    # window=2 trailing means: None (window not full), then (2+0)/2, ...
    assert [d["rolling_avg"] for d in series["days"]] == [None, 1.0, 0.0, 0.5]


def test_daily_series_uses_local_calendar_day(conn):
    # Both visits are 2025-01-01 in UTC but 2024-12-31 in Chicago
    add_page(conn, "Cat", ["2025-01-01T03:00:00Z", "2025-01-01T05:00:00Z"])
    series = stats.daily_series(conn, tz="America/Chicago")
    assert [d["date"] for d in series["days"]] == ["2024-12-31"]
    assert series["days"][0]["visits"] == 2


def test_daily_series_empty_db(conn):
    assert stats.daily_series(conn, tz="UTC")["days"] == []


# ---------------------------------------------------------------------------
# busiest_week
# ---------------------------------------------------------------------------

def test_busiest_week_finds_max_and_breaks_ties_earliest(conn):
    # ISO week 2025-W01 is Mon 2024-12-30 .. Sun 2025-01-05
    add_page(conn, "Cat", [
        "2025-01-01T10:00:00Z", "2025-01-02T10:00:00Z",   # W01: 2 visits
        "2025-01-07T10:00:00Z", "2025-01-08T10:00:00Z",   # W02: 2 visits (tie)
        "2025-02-01T10:00:00Z",                            # W05: 1 visit
    ])
    week = stats.busiest_week(conn, tz="UTC")
    assert week == {
        "timezone": "UTC",
        "iso_year": 2025,
        "iso_week": 1,
        "start_date": "2024-12-30",
        "end_date": "2025-01-05",
        "visits": 2,
    }


def test_busiest_week_empty_db(conn):
    assert stats.busiest_week(conn, tz="UTC") is None


# ---------------------------------------------------------------------------
# topic_distribution (unique pages, D2; unlabeled -> Unclassified)
# ---------------------------------------------------------------------------

def test_topic_distribution_fractions_over_unique_pages(conn):
    # Jainism has 2 visits but counts once: distribution is over pages
    add_page(conn, "Jainism", ["2025-01-01T10:00:00Z", "2025-01-01T11:00:00Z"],
             macro="Culture.Philosophy and religion",
             raw="Culture.Philosophy and religion")
    add_page(conn, "Physics", ["2025-01-02T10:00:00Z"],
             macro="STEM", raw="STEM.Physics")
    add_page(conn, "Chemistry", ["2025-01-03T10:00:00Z"],
             macro="STEM", raw="STEM.Chemistry")
    add_page(conn, "Mystery Page", ["2025-01-04T10:00:00Z"])  # no topic row

    dist = stats.topic_distribution(conn)
    assert dist["total_pages"] == 4
    assert dist["macro_topics"] == [
        {"macro_topic": "STEM", "pages": 2, "fraction": 0.5},
        {"macro_topic": "Culture.Philosophy and religion", "pages": 1,
         "fraction": 0.25},
        {"macro_topic": "Unclassified", "pages": 1, "fraction": 0.25},
    ]
    raw_labels = {r["raw_label"]: r["pages"] for r in dist["raw_labels"]}
    assert raw_labels == {
        "Culture.Philosophy and religion": 1,
        "STEM.Physics": 1,
        "STEM.Chemistry": 1,
        "Unclassified": 1,
    }


def test_topic_distribution_empty_db(conn):
    dist = stats.topic_distribution(conn)
    assert dist == {"total_pages": 0, "macro_topics": [], "raw_labels": []}
