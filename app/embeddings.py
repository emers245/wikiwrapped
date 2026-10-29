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
import os
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


# Hugging Face's own switches for "never touch the network". Read by
# huggingface_hub and transformers when they are first imported, which is
# why they are set before the import below.
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


class ModelMissing(RuntimeError):
    """The installed app's bundled model is not where it should be."""


def get_model():
    """Lazy singleton; loading sentence-transformers is expensive.

    Where the model comes from, in order:

    1. The bundled copy in config.EMBEDDING_MODEL_DIR (an installed app, or
       a checkout after scripts/fetch_model.py). Loading a local folder
       never contacts Hugging Face.
    2. Installed app without that copy: ModelMissing. An install is never
       allowed to quietly download a replacement, so the offline switches
       are set first, and a broken install says so instead.
    3. Dev checkout without it: download the pinned revision from Hugging
       Face (cached by huggingface_hub), as before bundling existed.

    Both routes use config.EMBEDDING_MODEL_REVISION, the exact weights the
    reference space was built with.
    """
    global _model
    if _model is None:
        local = config.EMBEDDING_MODEL_DIR
        installed = config.is_installed()
        if installed:
            for name, value in OFFLINE_ENV.items():
                os.environ.setdefault(name, value)
        if not (local / "modules.json").is_file() and installed:
            raise ModelMissing(
                f"The bundled language model is missing from {local}. "
                "Reinstall WikiWrapped to restore it."
            )
        from sentence_transformers import SentenceTransformer

        if (local / "modules.json").is_file():
            _model = SentenceTransformer(str(local), device="cpu")
        else:
            log.info(
                "No bundled model at %s; downloading %s@%s (dev fallback; "
                "scripts/fetch_model.py bundles it).",
                local, config.EMBEDDING_MODEL, config.EMBEDDING_MODEL_REVISION[:12],
            )
            _model = SentenceTransformer(
                config.EMBEDDING_MODEL, device="cpu",
                revision=config.EMBEDDING_MODEL_REVISION,
            )
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
    on_progress=None,
) -> dict:
    """Fetch summaries and cache embeddings for every title missing one.

    progress draws a tqdm bar (terminal use); on_progress(done, total), if
    given, is called after each committed chunk (the app window)."""
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
        if on_progress is not None:
            on_progress(report["embedded"], len(todo))
    return report


def embed_pages(
    conn: sqlite3.Connection, session=None, model=None, progress: bool = False,
    on_progress=None,
) -> dict:
    """Embed every visited page missing a vector (the /enrich entry point)."""
    report = embed_titles(
        conn, storage.all_page_titles(conn), session, model, progress,
        on_progress=on_progress,
    )
    return {
        "pages_embedded": report["embedded"],
        "pages_already_cached": report["already_cached"],
        "pages_empty_summary": report["empty_summaries"],
    }
