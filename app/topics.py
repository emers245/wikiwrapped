"""Lift Wing outlink-topic-model client + macro() collapsing (repo_spec
S7.2, D3).

ORES is deprecated; the Lift Wing model takes a page title directly, so the
old titles_to_revids step is gone. Response shape (verified live
2026-07-19):

    {"prediction": {"article": "...", "results": [{"topic": "...",
                                                   "score": 0.98}]}}

An article the model cannot classify (including nonexistent pages) returns
HTTP 200 with empty results. Labels arrive with underscores
("Culture.Philosophy_and_religion"); they are stored with spaces to match
the WikiProject taxonomy names the old ORES analysis used.

Caching (D6): one topic_cache row per canonical title. Unclassifiable
pages are cached with NULL labels so they are never re-fetched; stats
reports them as "Unclassified".
"""

from __future__ import annotations

import logging
import sqlite3
import time

import config
from app import storage, wiki_http

log = logging.getLogger(__name__)


def macro(label: str) -> str:
    """Collapse a fine-grained topic label to its macro bucket.

    Ported unchanged from the old sampler: first-level topic, except
    Culture.* keeps its second level.
    """
    parts = label.split(".")
    return parts[0] if parts[0] != "Culture" else ".".join(parts[:2])


def fetch_topic(title: str, session=None) -> str | None:
    """Top raw topic label for one canonical title, or None if the model
    cannot classify it."""
    session = session or wiki_http.make_session()
    response = wiki_http.request_with_retries(
        session,
        "post",
        config.LIFTWING_TOPIC_URL,
        json={"page_title": title.replace(" ", "_"), "lang": config.WIKI_LANG},
    )
    if response.status_code != 200:
        # 4xx (bad title, model refuses): unclassifiable, not fatal
        log.info("Lift Wing cannot classify %r (HTTP %d)", title,
                 response.status_code)
        return None
    results = response.json().get("prediction", {}).get("results", [])
    if not results:
        return None
    top = max(results, key=lambda entry: entry.get("score", 0.0))
    label = top.get("topic")
    return label.replace("_", " ") if label else None


def label_pages(conn: sqlite3.Connection, session=None) -> dict:
    """Fetch and cache topics for every visited page missing one (D6).

    Commits after each page so an interrupted run keeps its progress.
    """
    todo = storage.pages_without_topics(conn)
    already_cached = storage.count_pages(conn) - len(todo)
    session = session or wiki_http.make_session()

    labeled = 0
    unclassified = 0
    for index, title in enumerate(todo):
        raw_label = fetch_topic(title, session)
        if raw_label is None:
            storage.upsert_topic(conn, title, None, None)
            unclassified += 1
            log.info("unclassified: %s", title)
        else:
            storage.upsert_topic(conn, title, macro(raw_label), raw_label)
            labeled += 1
        conn.commit()
        if index < len(todo) - 1:
            time.sleep(config.THROTTLE_S)

    return {
        "pages_labeled": labeled,
        "pages_unclassified": unclassified,
        "pages_already_cached": already_cached,
    }
