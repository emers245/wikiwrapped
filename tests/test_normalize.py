"""Tests for app.normalize (repo_spec S7.1, D5).

url_to_title is pure and tested directly; resolve_redirects/canonicalize
are tested against a fake session with canned MediaWiki API payloads. One
live-API test is marked "integration" (deselect with -m "not integration").
"""

import pytest
import requests

import config
from app import normalize
from fakes import FakeResponse, FakeSession, api_payload


# ---------------------------------------------------------------------------
# url_to_title: pure URL parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Basic /wiki/ URL, underscores -> spaces
        ("https://en.wikipedia.org/wiki/Douglas_Adams", "Douglas Adams"),
        # Percent-encoding decoded (Sao Paulo with a tilde-a)
        ("https://en.wikipedia.org/wiki/S%C3%A3o_Paulo", "S\u00e3o Paulo"),
        # Percent-encoded punctuation: apostrophe and plus signs
        (
            "https://en.wikipedia.org/wiki/Hitchhiker%27s_Guide_to_the_Galaxy",
            "Hitchhiker's Guide to the Galaxy",
        ),
        ("https://en.wikipedia.org/wiki/C%2B%2B", "C++"),
        # Section fragment stripped
        ("https://en.wikipedia.org/wiki/Douglas_Adams#Biography", "Douglas Adams"),
        # Percent-encoded "#" also cannot be part of a title
        ("https://en.wikipedia.org/wiki/Douglas_Adams%23Biography", "Douglas Adams"),
        # Query params ignored
        ("https://en.wikipedia.org/wiki/Douglas_Adams?wprov=sfla1", "Douglas Adams"),
        # Old-style index.php URLs carry the title as a query param
        (
            "https://en.wikipedia.org/w/index.php?title=Douglas_Adams&action=history",
            "Douglas Adams",
        ),
        # Mobile host accepted
        ("https://en.m.wikipedia.org/wiki/Douglas_Adams", "Douglas Adams"),
        # First letter uppercased (MediaWiki titles are stored that way)
        ("https://en.wikipedia.org/wiki/douglas_adams", "Douglas adams"),
        # Colon in title is fine when the prefix is not a real namespace
        ("https://en.wikipedia.org/wiki/Star_Trek:_Voyager", "Star Trek: Voyager"),
        # Repeated separators collapse
        ("https://en.wikipedia.org/wiki/Douglas__Adams", "Douglas Adams"),
    ],
)
def test_url_to_title_articles(url, expected):
    assert normalize.url_to_title(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Non-article namespaces (S7.1) incl. talk and encoded variants
        "https://en.wikipedia.org/wiki/File:Example.jpg",
        "https://en.wikipedia.org/wiki/Special:Random",
        "https://en.wikipedia.org/wiki/Help:Contents",
        "https://en.wikipedia.org/wiki/Talk:Douglas_Adams",
        "https://en.wikipedia.org/wiki/Category:English_writers",
        "https://en.wikipedia.org/wiki/Template:Infobox",
        "https://en.wikipedia.org/wiki/Portal:Science",
        "https://en.wikipedia.org/wiki/Wikipedia:About",
        "https://en.wikipedia.org/wiki/Draft:Something",
        "https://en.wikipedia.org/wiki/User:Jimbo_Wales",
        "https://en.m.wikipedia.org/wiki/Special:MobileOptions",
        "https://en.wikipedia.org/wiki/Talk%3ADouglas_Adams",
        # Wrong host / wrong language / not an article path
        "https://de.wikipedia.org/wiki/Douglas_Adams",
        "https://www.wikipedia.org/",
        "https://en.wikipedia.beta.wmflabs.org/wiki/Douglas_Adams",
        "https://upload.wikimedia.org/wikipedia/commons/a/a1/Foo.jpg",
        "https://commons.wikimedia.org/wiki/File:Foo.jpg",
        "https://www.google.com/search?q=wikipedia",
        "https://en.wikipedia.org/",
        "https://en.wikipedia.org/wiki/",
        "https://en.wikipedia.org/w/index.php?action=history",
        "https://en.wikipedia.org/w/load.php?modules=startup",
        "not a url at all",
        "",
    ],
)
def test_url_to_title_non_articles(url):
    assert normalize.url_to_title(url) is None


# ---------------------------------------------------------------------------
# Fake MediaWiki API plumbing lives in tests/fakes.py (shared with ingest)
# ---------------------------------------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(normalize.time, "sleep", sleeps.append)
    return sleeps


# ---------------------------------------------------------------------------
# resolve_redirects
# ---------------------------------------------------------------------------

def test_resolve_redirects_follows_redirects_and_normalization():
    session = FakeSession([
        FakeResponse(api_payload(
            normalized=[("douglas adams", "Douglas adams")],
            redirects=[("Douglas adams", "Douglas Adams"),
                       ("HHGTTG", "The Hitchhiker's Guide to the Galaxy")],
            pages=["Douglas Adams", "The Hitchhiker's Guide to the Galaxy"],
        )),
    ])
    result = normalize.resolve_redirects(["douglas adams", "HHGTTG"], session)
    assert result == {
        "douglas adams": "Douglas Adams",
        "HHGTTG": "The Hitchhiker's Guide to the Galaxy",
    }


def test_resolve_redirects_follows_chains():
    session = FakeSession([
        FakeResponse(api_payload(redirects=[("A", "B"), ("B", "C")], pages=["C"])),
    ])
    assert normalize.resolve_redirects(["A"], session) == {"A": "C"}


def test_resolve_redirects_cycle_is_dead_end():
    session = FakeSession([
        FakeResponse(api_payload(redirects=[("A", "B"), ("B", "A")])),
    ])
    assert normalize.resolve_redirects(["A"], session) == {"A": None}


def test_resolve_redirects_missing_and_invalid_map_to_none():
    session = FakeSession([
        FakeResponse(api_payload(
            pages=["Douglas Adams"],
            missing=["No Such Page Xyz"],
            invalid=["<bad>"],
        )),
    ])
    result = normalize.resolve_redirects(
        ["Douglas Adams", "No Such Page Xyz", "<bad>"], session
    )
    assert result == {
        "Douglas Adams": "Douglas Adams",
        "No Such Page Xyz": None,
        "<bad>": None,
    }


def test_resolve_redirects_batches_of_50(no_sleep):
    titles = [f"Title {i}" for i in range(120)]
    session = FakeSession([
        FakeResponse(api_payload(pages=titles[0:50])),
        FakeResponse(api_payload(pages=titles[50:100])),
        FakeResponse(api_payload(pages=titles[100:120])),
    ])
    result = normalize.resolve_redirects(titles, session)
    assert len(session.calls) == 3
    assert all(len(call["titles"].split("|")) <= 50 for call in session.calls)
    assert result == {t: t for t in titles}
    # Polite throttle between batches, not after the last one
    assert no_sleep == [config.THROTTLE_S, config.THROTTLE_S]


def test_resolve_redirects_dedupes_input():
    session = FakeSession([FakeResponse(api_payload(pages=["Douglas Adams"]))])
    normalize.resolve_redirects(["Douglas Adams"] * 5, session)
    assert session.calls[0]["titles"] == "Douglas Adams"


def test_resolve_redirects_pipe_title_skipped_without_api_call():
    session = FakeSession([])
    assert normalize.resolve_redirects(["bad|title"], session) == {"bad|title": None}
    assert session.calls == []


def test_resolve_redirects_retries_on_429_with_backoff(no_sleep):
    session = FakeSession([
        FakeResponse(status_code=429),
        FakeResponse(status_code=503),
        FakeResponse(api_payload(pages=["Douglas Adams"])),
    ])
    result = normalize.resolve_redirects(["Douglas Adams"], session)
    assert result == {"Douglas Adams": "Douglas Adams"}
    assert len(session.calls) == 3
    assert no_sleep == [config.BACKOFF_BASE_S, config.BACKOFF_BASE_S * 2]


def test_resolve_redirects_gives_up_after_max_retries(no_sleep):
    session = FakeSession(
        [FakeResponse(status_code=429)] * (config.MAX_RETRIES + 1)
    )
    with pytest.raises(requests.HTTPError):
        normalize.resolve_redirects(["Douglas Adams"], session)
    assert len(session.calls) == config.MAX_RETRIES + 1


def test_resolve_redirects_no_retry_on_client_error(no_sleep):
    session = FakeSession([FakeResponse(status_code=400)])
    with pytest.raises(requests.HTTPError):
        normalize.resolve_redirects(["Douglas Adams"], session)
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# canonicalize
# ---------------------------------------------------------------------------

def test_canonicalize_end_to_end():
    session = FakeSession([
        FakeResponse(api_payload(
            redirects=[("HHGTTG", "The Hitchhiker's Guide to the Galaxy")],
            pages=["Douglas Adams", "The Hitchhiker's Guide to the Galaxy"],
        )),
    ])
    cache = {}
    urls = [
        "https://en.wikipedia.org/wiki/Douglas_Adams",
        "https://en.wikipedia.org/wiki/Douglas_Adams#Works",   # same page
        "https://en.m.wikipedia.org/wiki/HHGTTG",              # redirect
        "https://en.wikipedia.org/wiki/File:Example.jpg",      # non-article
        "https://www.google.com/",                             # non-wiki
    ]
    result = normalize.canonicalize(urls, cache, session)
    assert result == {
        urls[0]: "Douglas Adams",
        urls[1]: "Douglas Adams",
        urls[2]: "The Hitchhiker's Guide to the Galaxy",
        urls[3]: None,
        urls[4]: None,
    }
    assert cache == {
        "Douglas Adams": "Douglas Adams",
        "HHGTTG": "The Hitchhiker's Guide to the Galaxy",
    }
    # Both /wiki/Douglas_Adams URLs collapse to one title: one API batch
    assert len(session.calls) == 1


def test_canonicalize_uses_cache_without_network():
    session = FakeSession([])  # any request would raise IndexError
    cache = {"Douglas Adams": "Douglas Adams", "HHGTTG": None}
    result = normalize.canonicalize(
        ["https://en.wikipedia.org/wiki/Douglas_Adams",
         "https://en.wikipedia.org/wiki/HHGTTG"],
        cache,
        session,
    )
    assert result == {
        "https://en.wikipedia.org/wiki/Douglas_Adams": "Douglas Adams",
        "https://en.wikipedia.org/wiki/HHGTTG": None,
    }
    assert session.calls == []


# ---------------------------------------------------------------------------
# Live API (skipped offline: pytest -m "not integration")
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_live_redirect_resolution():
    result = normalize.canonicalize(
        ["https://en.m.wikipedia.org/wiki/USA",
         "https://en.wikipedia.org/wiki/S%C3%A3o_Paulo"],
    )
    assert result["https://en.m.wikipedia.org/wiki/USA"] == "United States"
    assert result["https://en.wikipedia.org/wiki/S%C3%A3o_Paulo"] == "S\u00e3o Paulo"
