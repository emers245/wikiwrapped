"""Tests for app.pageviews (repo_spec S7.6, D7, D6): range derivation from
the user's history, daily-sum parsing, 404-as-no-data caching, and
cache-hit behavior."""

import pytest

from app import pageviews, storage
from fakes import FakeResponse, FakeSession


@pytest.fixture
def conn():
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(pageviews.time, "sleep", lambda seconds: None)


def add_page(conn, title, timestamps):
    page_id = storage.upsert_page(conn, title, title, min(timestamps))
    storage.insert_visits(conn, page_id, timestamps)
    storage.refresh_visit_counts(conn)


def views_response(*daily):
    return FakeResponse({"items": [{"views": v} for v in daily]})


def test_history_date_range_matches_visits(conn):
    add_page(conn, "Cat", ["2025-04-25T18:32:18Z", "2025-06-28T22:46:45Z"])
    assert pageviews.history_date_range(conn) == ("20250425", "20250628")
    empty = storage.connect(":memory:")
    assert pageviews.history_date_range(empty) is None


def test_fetch_pageviews_sums_daily_and_encodes_title():
    session = FakeSession([views_response(10, 20, 5)])
    total = pageviews.fetch_pageviews(
        "Jain vegetarianism / test?", "20250101", "20250131", session
    )
    assert total == 35
    # FakeSession.get records params (None for a bare URL GET)
    assert session.calls == [None]


def test_fetch_pageviews_404_is_none():
    session = FakeSession([FakeResponse(status_code=404)])
    assert pageviews.fetch_pageviews("Gone", "20250101", "20250131", session) is None


def test_get_pageviews_caches_including_no_data(conn):
    result = pageviews.get_pageviews(
        conn, "Cat", "20250101", "20250131",
        FakeSession([views_response(7, 3)]),
    )
    assert result["total_views"] == 10 and not result["no_data"]

    nodata = pageviews.get_pageviews(
        conn, "Gone", "20250101", "20250131",
        FakeSession([FakeResponse(status_code=404)]),
    )
    assert nodata["total_views"] == 0 and nodata["no_data"]

    # Both served from cache now: any request would raise IndexError
    silent = FakeSession([])
    assert pageviews.get_pageviews(
        conn, "Cat", "20250101", "20250131", silent)["total_views"] == 10
    assert pageviews.get_pageviews(
        conn, "Gone", "20250101", "20250131", silent)["no_data"]
    assert silent.calls == []


def test_range_change_misses_cache(conn):
    pageviews.get_pageviews(conn, "Cat", "20250101", "20250131",
                            FakeSession([views_response(1)]))
    session = FakeSession([views_response(2)])
    result = pageviews.get_pageviews(conn, "Cat", "20250101", "20250228", session)
    assert result["total_views"] == 2
    assert len(session.calls) == 1  # different range -> refetched (D7)


def test_fetch_all_covers_pages_and_resumes(conn):
    add_page(conn, "Cat", ["2025-01-01T10:00:00Z"])
    add_page(conn, "Gone", ["2025-01-05T10:00:00Z"])
    report = pageviews.fetch_all(conn, FakeSession([
        views_response(4, 6),               # Cat (alphabetical order)
        FakeResponse(status_code=404),      # Gone
    ]))
    assert report == {
        "pages_fetched": 1,
        "pages_no_data": 1,
        "pages_already_cached": 0,
        "date_range": ["20250101", "20250105"],
    }

    silent = FakeSession([])
    report = pageviews.fetch_all(conn, silent)
    assert silent.calls == []
    assert report["pages_already_cached"] == 2
