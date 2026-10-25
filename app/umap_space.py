"""Project visited articles into the frozen UMAP reference space (repo_spec
S7.5, D4).

The reference space is fitted ONCE by scripts/build_reference_space.py and
persisted as a read-only app resource (config.REFERENCE_DIR, data/reference/
in a dev checkout); this module only loads the frozen reducer
and .transform()s visited-article embeddings into it - never refits, which
is what makes layouts reproducible across runs. Coordinates land in
umap_coords.

UMAP parameters live here (single source of truth for the build script):
ported from the old exploratory fit that produced good topic separation.
"""

from __future__ import annotations

import json
import logging
import os
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
    """Unpickle the frozen reducer.

    Unpickling imports umap, which JIT-compiles with numba, and numba writes
    its compile cache next to the library's own source files by default. In
    an installed app that folder is read-only, so the cache is pointed at
    the user-data dir first. setdefault leaves a developer's own setting
    alone, and it is done here, at the one place numba is triggered, rather
    than at import time.
    """
    numba_cache = Path(config.CACHE_DIR) / "numba"
    if "NUMBA_CACHE_DIR" not in os.environ:
        numba_cache.mkdir(parents=True, exist_ok=True)
        os.environ["NUMBA_CACHE_DIR"] = str(numba_cache)
    with open(_artifact(reference_dir, REDUCER_FILE), "rb") as handle:
        reducer = pickle.load(handle)
    # The build fits with verbose=True for its own console, and that choice
    # is pickled with the reducer twice over: the verbose flag (epoch
    # counters) and tqdm_kwds["disable"], which umap fills in from it at fit
    # time (the progress bar). Left on, every transform would print both
    # into the app log.
    reducer.verbose = False
    if isinstance(getattr(reducer, "tqdm_kwds", None), dict):
        reducer.tqdm_kwds["disable"] = True
    return reducer


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
