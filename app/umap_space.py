"""Project visited articles into the frozen UMAP reference space (repo_spec
S7.5, D4).

The reference space is fitted ONCE by scripts/build_reference_space.py and
persisted to data/reference/; this module only loads the frozen reducer
and .transform()s visited-article embeddings into it - never refits, which
is what makes layouts reproducible across runs. Coordinates land in
umap_coords.

UMAP parameters live here (single source of truth for the build script):
ported from the old exploratory fit that produced good topic separation.
"""

from __future__ import annotations

import json
import logging
import pickle
import sqlite3
from pathlib import Path

import numpy as np

import config
from app import storage

log = logging.getLogger(__name__)

UMAP_PARAMS = {
    "n_neighbors": 30,
    "min_dist": 0.1,
    "n_components": 2,
    "metric": "cosine",
    "random_state": 42,
}

REDUCER_FILE = "umap_reducer.pkl"
EMBEDDINGS_FILE = "reference_embeddings.npy"
POINTS_FILE = "reference_points.json"
META_FILE = "reference_meta.json"


def _artifact(reference_dir: str | Path | None, name: str) -> Path:
    directory = Path(reference_dir) if reference_dir else config.REFERENCE_DIR
    path = directory / name
    if not path.exists():
        raise FileNotFoundError(
            f"Reference-space artifact missing: {path}. Build the frozen "
            "space first with scripts/build_reference_space.py (D4)."
        )
    return path


def load_reducer(reference_dir: str | Path | None = None):
    with open(_artifact(reference_dir, REDUCER_FILE), "rb") as handle:
        return pickle.load(handle)


def load_points(reference_dir: str | Path | None = None) -> list[dict]:
    """Reference points: [{title, macro_topic, x, y, source}, ...]."""
    return json.loads(
        _artifact(reference_dir, POINTS_FILE).read_text(encoding="utf-8")
    )


def load_meta(reference_dir: str | Path | None = None) -> dict:
    return json.loads(
        _artifact(reference_dir, META_FILE).read_text(encoding="utf-8")
    )


def project_pages(
    conn: sqlite3.Connection, reference_dir: str | Path | None = None
) -> dict:
    """Transform visited pages' cached embeddings into the frozen space.

    Pages without a cached embedding are reported, not projected - run
    embeddings.embed_pages first (the /enrich flow does).
    """
    todo = storage.pages_without_coords(conn)
    already = storage.count_pages(conn) - len(todo)
    report = {
        "pages_projected": 0,
        "pages_missing_embedding": 0,
        "pages_already_projected": already,
    }
    if not todo:
        return report

    vectors = storage.get_embeddings(conn, config.EMBEDDING_MODEL, todo)
    ready = [title for title in todo if title in vectors]
    missing = [title for title in todo if title not in vectors]
    for title in missing:
        log.warning("no cached embedding, cannot project: %s", title)

    if ready:
        reducer = load_reducer(reference_dir)
        coords = reducer.transform(np.vstack([vectors[t] for t in ready]))
        storage.upsert_umap_coords(
            conn,
            {
                title: (float(x), float(y))
                for title, (x, y) in zip(ready, coords)
            },
        )
        conn.commit()

    report["pages_projected"] = len(ready)
    report["pages_missing_embedding"] = len(missing)
    return report
