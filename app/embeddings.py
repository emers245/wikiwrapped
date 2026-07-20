"""Article summary fetch + sentence-transformer encoding (repo_spec S7.4).

Payload per article is the REST summary's description + extract joined,
matching the old pipeline; if both are missing (rare: deleted or empty
pages), the title itself is embedded instead so the vector is never
degenerate. Vectors are unit-normalized (cosine == Euclidean downstream).

Caching (D6): embedding_cache keyed by (canonical_title, model_name); a
cached title costs neither a summary fetch nor an encode. Work happens in
chunks with a commit per chunk, so interrupted bulk runs (the 40k
reference build) resume nearly where they stopped.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from urllib.parse import quote

import numpy as np
from tqdm import tqdm

import config
from app import storage, wiki_http

log = logging.getLogger(__name__)

CHUNK_SIZE = 64

_model = None


def get_model():
    """Lazy singleton; loading sentence-transformers is expensive."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(config.EMBEDDING_MODEL, device="cpu")
    return _model


def fetch_summary(title: str, session=None) -> dict:
    """description + extract from the REST summary endpoint; both None on
    404 (recorded by the caller so the page is not retried forever)."""
    session = session or wiki_http.make_session()
    url = config.SUMMARY_API_URL + quote(title.replace(" ", "_"), safe="")
    response = wiki_http.request_with_retries(session, "get", url)
    if response.status_code == 404:
        return {"description": None, "extract": None}
    response.raise_for_status()
    data = response.json()
    return {"description": data.get("description"), "extract": data.get("extract")}


def summary_payload(title: str, summary: dict) -> str:
    """Text to embed for one article; falls back to the title itself."""
    text = " ".join(
        part
        for part in (summary.get("description"), summary.get("extract"))
        if part
    ).strip()
    return text or title


def embed_titles(
    conn: sqlite3.Connection,
    titles: list[str],
    session=None,
    model=None,
    progress: bool = False,
) -> dict:
    """Fetch summaries and cache embeddings for every title missing one."""
    unique = list(dict.fromkeys(t for t in titles if t))
    have = storage.embedded_titles(conn, config.EMBEDDING_MODEL)
    todo = [t for t in unique if t not in have]
    report = {
        "embedded": 0,
        "already_cached": len(unique) - len(todo),
        "empty_summaries": 0,
    }
    if not todo:
        return report

    session = session or wiki_http.make_session()
    model = model or get_model()
    starts = range(0, len(todo), CHUNK_SIZE)
    if progress:
        starts = tqdm(starts, desc="embeddings", unit="chunk")
    for start in starts:
        chunk = todo[start:start + CHUNK_SIZE]
        payloads = []
        for title in chunk:
            summary = fetch_summary(title, session)
            if not summary["description"] and not summary["extract"]:
                report["empty_summaries"] += 1
                log.info("no summary text, embedding title only: %s", title)
            payloads.append(summary_payload(title, summary))
            time.sleep(config.THROTTLE_S)
        vectors = model.encode(
            payloads,
            batch_size=CHUNK_SIZE,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        for title, vector in zip(chunk, vectors):
            storage.upsert_embedding(conn, title, config.EMBEDDING_MODEL, vector)
        conn.commit()
        report["embedded"] += len(chunk)
    return report


def embed_pages(
    conn: sqlite3.Connection, session=None, model=None, progress: bool = False
) -> dict:
    """Embed every visited page missing a vector (the /enrich entry point)."""
    report = embed_titles(
        conn, storage.all_page_titles(conn), session, model, progress
    )
    return {
        "pages_embedded": report["embedded"],
        "pages_already_cached": report["already_cached"],
        "pages_empty_summary": report["empty_summaries"],
    }
