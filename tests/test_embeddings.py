"""Tests for app.embeddings (repo_spec S7.4, D6): summary parsing, payload
fallback, BLOB round-trip, and cache-hit behavior. The sentence-transformer
is faked (loading the real model is a heavyweight operation covered by the
live build verification)."""

import numpy as np
import pytest

import config
from app import embeddings, storage
from fakes import FakeResponse, FakeSession


class FakeModel:
    """Deterministic stand-in for SentenceTransformer.encode."""

    def __init__(self, dim=8):
        self.dim = dim
        self.encoded = []

    def encode(self, payloads, **kwargs):
        self.encoded.extend(payloads)
        vectors = []
        for payload in payloads:
            raw = np.arange(self.dim, dtype=np.float32) + len(payload)
            vectors.append(raw / np.linalg.norm(raw))
        return np.vstack(vectors)


@pytest.fixture
def conn():
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)


def add_page(conn, title):
    page_id = storage.upsert_page(conn, title, title, "2025-01-01T00:00:00Z")
    storage.insert_visits(conn, page_id, ["2025-01-01T00:00:00Z"])


def summary_response(description, extract):
    return FakeResponse({"description": description, "extract": extract,
                         "title": "whatever"})


# ---------------------------------------------------------------------------
# fetch_summary / summary_payload
# ---------------------------------------------------------------------------

def test_fetch_summary_returns_text_fields():
    session = FakeSession([summary_response("English author", "Douglas...")])
    assert embeddings.fetch_summary("Douglas Adams", session) == {
        "description": "English author",
        "extract": "Douglas...",
    }


def test_fetch_summary_404_is_empty_not_fatal():
    session = FakeSession([FakeResponse(status_code=404)])
    assert embeddings.fetch_summary("Gone Page", session) == {
        "description": None,
        "extract": None,
    }


def test_summary_payload_joins_and_falls_back_to_title():
    assert embeddings.summary_payload(
        "X", {"description": "a bird", "extract": "The bird..."}
    ) == "a bird The bird..."
    assert embeddings.summary_payload(
        "Douglas Adams", {"description": None, "extract": None}
    ) == "Douglas Adams"


# ---------------------------------------------------------------------------
# embed_titles: caching (D6) + storage round-trip
# ---------------------------------------------------------------------------

def test_embed_titles_fetches_encodes_and_caches(conn):
    model = FakeModel()
    session = FakeSession([
        summary_response("English author", "Douglas Adams was..."),
        summary_response(None, None),  # no text: embeds the title itself
    ])
    report = embeddings.embed_titles(
        conn, ["Douglas Adams", "Obscure Page"], session, model
    )
    assert report == {"embedded": 2, "already_cached": 0, "empty_summaries": 1}
    assert model.encoded == [
        "English author Douglas Adams was...",
        "Obscure Page",
    ]

    vectors = storage.get_embeddings(
        conn, config.EMBEDDING_MODEL, ["Douglas Adams", "Obscure Page"]
    )
    assert set(vectors) == {"Douglas Adams", "Obscure Page"}
    vector = vectors["Douglas Adams"]
    assert vector.dtype == np.float32 and vector.shape == (8,)
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-5)
    # Round-trip is exact: same payload re-encoded gives the same bytes
    assert np.array_equal(vector, model.encode(
        ["English author Douglas Adams was..."])[0].astype(np.float32))


def test_embed_titles_cache_hit_makes_no_calls(conn):
    model = FakeModel()
    embeddings.embed_titles(
        conn, ["Douglas Adams"], FakeSession([summary_response("a", "b")]), model
    )

    silent_session = FakeSession([])  # a request would raise IndexError
    silent_model = FakeModel()
    report = embeddings.embed_titles(
        conn, ["Douglas Adams"], silent_session, silent_model
    )
    assert report == {"embedded": 0, "already_cached": 1, "empty_summaries": 0}
    assert silent_session.calls == []
    assert silent_model.encoded == []


def test_embed_pages_covers_visited_pages(conn):
    add_page(conn, "Cat")
    add_page(conn, "Dog")
    report = embeddings.embed_pages(
        conn,
        FakeSession([summary_response("feline", "x"),
                     summary_response("canine", "y")]),
        FakeModel(),
    )
    assert report == {
        "pages_embedded": 2,
        "pages_already_cached": 0,
        "pages_empty_summary": 0,
    }
