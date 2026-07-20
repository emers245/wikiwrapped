"""Tests for app.umap_space (repo_spec S7.5, D4) and the build script's
sampling helpers. The fitted reducer is stubbed: transform-correctness of
the real reducer is covered by the build verification (a held-out
reference point projects near its fitted position)."""

import gzip
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

import config
from app import storage, umap_space
from fakes import FakeResponse, FakeSession

ROOT = Path(__file__).resolve().parents[1]


def load_build_script():
    spec = importlib.util.spec_from_file_location(
        "build_reference_space", ROOT / "scripts" / "build_reference_space.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build():
    return load_build_script()


@pytest.fixture
def conn():
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


class StubReducer:
    """transform() echoes back deterministic coordinates."""

    def transform(self, matrix):
        return np.array([[float(i), -float(i)] for i in range(len(matrix))])


def add_embedded_page(conn, title, seed):
    page_id = storage.upsert_page(conn, title, title, "2025-01-01T00:00:00Z")
    storage.insert_visits(conn, page_id, ["2025-01-01T00:00:00Z"])
    vector = np.full(8, float(seed), dtype=np.float32)
    storage.upsert_embedding(conn, title, config.EMBEDDING_MODEL, vector)


# ---------------------------------------------------------------------------
# project_pages (D4: transform only, never refit)
# ---------------------------------------------------------------------------

def test_project_pages_stores_coords_and_reports_missing(conn, monkeypatch):
    monkeypatch.setattr(umap_space, "load_reducer", lambda d=None: StubReducer())
    add_embedded_page(conn, "Ant", 1)
    add_embedded_page(conn, "Bee", 2)
    page_id = storage.upsert_page(conn, "Cat", "Cat", "2025-01-01T00:00:00Z")
    storage.insert_visits(conn, page_id, ["2025-01-01T00:00:00Z"])  # no vector

    report = umap_space.project_pages(conn)
    assert report == {
        "pages_projected": 2,
        "pages_missing_embedding": 1,
        "pages_already_projected": 0,
    }
    assert storage.get_umap_coords(conn) == {
        "Ant": (0.0, -0.0), "Bee": (1.0, -1.0),
    }

    # Second run: projected pages are cached; only Cat is still pending
    report = umap_space.project_pages(conn)
    assert report == {
        "pages_projected": 0,
        "pages_missing_embedding": 1,
        "pages_already_projected": 2,
    }


def test_missing_artifacts_point_at_build_script(tmp_path):
    with pytest.raises(FileNotFoundError, match="build_reference_space"):
        umap_space.load_reducer(tmp_path)


# ---------------------------------------------------------------------------
# build script helpers
# ---------------------------------------------------------------------------

def test_parse_dump_filters_and_ranks(build, tmp_path):
    lines = [
        "en Jainism 500 0",
        "en Special:Search 9999 0",       # namespace: excluded
        "en Main_Page 99999 0",           # excluded
        "de Berlin 800 0",                # wrong project
        "en Douglas_Adams 700 0",
        "en S%C3%A3o_Paulo 600 0",        # percent-encoded
        "en Jainism 100 0",               # duplicate title
        "malformed line",
        "en BadViews x 0",
    ]
    dump = tmp_path / "pageviews.gz"
    with gzip.open(dump, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    top = build.parse_dump(dump, 3)
    assert top == ["Douglas Adams", "S\u00e3o Paulo", "Jainism"]


def test_sample_random_paginates_dedupes_and_excludes(build, monkeypatch):
    def payload(*titles):
        return FakeResponse(
            {"query": {"random": [{"title": t} for t in titles]}}
        )

    session = FakeSession([
        payload("Alpha", "Beta", "Excluded One"),
        payload("Beta", "Gamma", "Delta"),
    ])
    monkeypatch.setattr(build.time, "sleep", lambda seconds: None)
    titles = build.sample_random(4, session, exclude={"Excluded One"})
    assert titles == ["Alpha", "Beta", "Gamma", "Delta"]
    assert session.calls[0]["list"] == "random"
    assert session.calls[0]["rnnamespace"] == 0
