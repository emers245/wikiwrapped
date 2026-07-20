"""Tests for app.topics (repo_spec S7.2, D3, D6): macro() collapsing,
Lift Wing response parsing, backoff behavior, and cache-hit behavior of
label_pages. Live smoke test marked "integration"."""

import pytest
import requests

import config
from app import storage, topics, wiki_http
from fakes import FakeResponse, FakeSession


@pytest.fixture
def conn():
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(topics.time, "sleep", sleeps.append)
    monkeypatch.setattr(wiki_http.time, "sleep", sleeps.append)
    return sleeps


def liftwing_payload(*results):
    return {
        "prediction": {
            "article": "https://en.wikipedia.org/wiki/X",
            "results": [{"topic": t, "score": s} for t, s in results],
        }
    }


def add_page(conn, title):
    page_id = storage.upsert_page(conn, title, title, "2025-01-01T00:00:00Z")
    storage.insert_visits(conn, page_id, ["2025-01-01T00:00:00Z"])
    storage.refresh_visit_counts(conn)


# ---------------------------------------------------------------------------
# macro(): ported collapsing rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("STEM.Physics", "STEM"),
        ("STEM.STEM*", "STEM"),
        ("Geography.Regions.Asia", "Geography"),
        ("History and Society.Politics and government", "History and Society"),
        ("Culture.Media.Films", "Culture.Media"),
        ("Culture.Biography.Biography*", "Culture.Biography"),
        ("Culture.Philosophy and religion", "Culture.Philosophy and religion"),
        ("Geography", "Geography"),
    ],
)
def test_macro_collapsing(label, expected):
    assert topics.macro(label) == expected


# ---------------------------------------------------------------------------
# fetch_topic: parsing + errors
# ---------------------------------------------------------------------------

def test_fetch_topic_takes_top_score_and_restores_spaces():
    session = FakeSession([FakeResponse(liftwing_payload(
        ("Culture.Literature", 0.61),
        ("Culture.Philosophy_and_religion", 0.98),
    ))])
    assert topics.fetch_topic("Jainism", session) == \
        "Culture.Philosophy and religion"
    # Title goes out with underscores, per the API contract
    assert session.calls == [{"page_title": "Jainism", "lang": config.WIKI_LANG}]


def test_fetch_topic_sends_underscored_title():
    session = FakeSession([FakeResponse(liftwing_payload(("STEM.Physics", 0.9)))])
    topics.fetch_topic("Douglas Adams", session)
    assert session.calls[0]["page_title"] == "Douglas_Adams"


def test_fetch_topic_empty_results_is_unclassifiable():
    session = FakeSession([FakeResponse(liftwing_payload())])
    assert topics.fetch_topic("Zzz No Such Page", session) is None


def test_fetch_topic_4xx_is_unclassifiable_not_fatal():
    session = FakeSession([FakeResponse(status_code=400)])
    assert topics.fetch_topic("<weird>", session) is None


def test_fetch_topic_retries_on_429(no_sleep):
    session = FakeSession([
        FakeResponse(status_code=429),
        FakeResponse(liftwing_payload(("STEM.Physics", 0.9))),
    ])
    assert topics.fetch_topic("Physics", session) == "STEM.Physics"
    assert no_sleep == [config.BACKOFF_BASE_S]


def test_fetch_topic_gives_up_after_max_retries(no_sleep):
    session = FakeSession(
        [FakeResponse(status_code=503)] * (config.MAX_RETRIES + 1)
    )
    with pytest.raises(requests.HTTPError):
        topics.fetch_topic("Physics", session)


# ---------------------------------------------------------------------------
# label_pages: caching (D6)
# ---------------------------------------------------------------------------

def test_label_pages_fills_cache_and_reports(conn, no_sleep):
    add_page(conn, "Jainism")
    add_page(conn, "Physics")
    add_page(conn, "Zzz No Such Page")
    session = FakeSession([  # todo list is alphabetical
        FakeResponse(liftwing_payload(("Culture.Philosophy_and_religion", 0.98))),
        FakeResponse(liftwing_payload(("STEM.Physics", 0.9))),
        FakeResponse(liftwing_payload()),  # unclassifiable
    ])
    report = topics.label_pages(conn, session)
    assert report == {
        "pages_labeled": 2,
        "pages_unclassified": 1,
        "pages_already_cached": 0,
    }
    rows = {
        row["canonical_title"]: (row["macro_topic"], row["raw_label"])
        for row in conn.execute("SELECT * FROM topic_cache")
    }
    assert rows == {
        "Jainism": ("Culture.Philosophy and religion",
                    "Culture.Philosophy and religion"),
        "Physics": ("STEM", "STEM.Physics"),
        "Zzz No Such Page": (None, None),
    }


def test_label_pages_skips_cached_including_unclassifiable(conn, no_sleep):
    add_page(conn, "Jainism")
    add_page(conn, "Zzz No Such Page")
    topics.label_pages(conn, FakeSession([
        FakeResponse(liftwing_payload(("Culture.Philosophy_and_religion", 0.98))),
        FakeResponse(liftwing_payload()),
    ]))

    silent = FakeSession([])  # any request would raise IndexError
    report = topics.label_pages(conn, silent)
    assert silent.calls == []
    assert report == {
        "pages_labeled": 0,
        "pages_unclassified": 0,
        "pages_already_cached": 2,
    }


# ---------------------------------------------------------------------------
# Live API (integration)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_live_liftwing_labels():
    label = topics.fetch_topic("Jainism")
    assert label == "Culture.Philosophy and religion"
    assert topics.macro(label) == "Culture.Philosophy and religion"
    assert topics.fetch_topic("Zzz No Such Page Qqq") is None
