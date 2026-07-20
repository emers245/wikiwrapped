"""Load standardized history JSON into the DB (repo_spec S5, D1, D2).

Flow: validate schema_version -> extract raw titles (app.normalize) ->
resolve redirects through the persistent redirect_cache (only unseen
titles hit the network, D6) -> upsert pages, insert visits. Idempotent:
the (page_id, timestamp) uniqueness in storage means re-ingesting the same
or an updated export never duplicates visits; only what is new lands.

Dropped rows are counted and logged, never silent (D5): a URL drops when
it is not an article page (File:/Special:/other hosts) or its title does
not resolve to an existing article.

CLI for script-level verification before the API exists (Step 10):

    python -m app.ingest wikipedia_history.json [--db path/to/app.db]
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import config
from app import normalize, storage

log = logging.getLogger(__name__)

MAX_LOGGED_DROPS = 20


def ingest_file(
    path: str | Path,
    conn: sqlite3.Connection,
    session=None,
) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return ingest_document(document, conn, session=session)


def ingest_document(
    document: dict,
    conn: sqlite3.Connection,
    session=None,
) -> dict:
    """Ingest one standardized export; returns a report of what happened."""
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version != config.SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported schema_version {version!r}; "
            f"expected {config.SCHEMA_VERSION!r}. Re-run the extractor."
        )

    rows = document.get("visits", [])
    valid = [
        (row["url"], row["timestamp"])
        for row in rows
        if isinstance(row, dict) and row.get("url") and row.get("timestamp")
    ]
    malformed = len(rows) - len(valid)

    # Resolve URL -> canonical title, network only for cache misses (D6).
    cache = storage.get_redirect_cache(conn)
    known_before = set(cache)
    urls = [url for url, _ in valid]
    mapping = normalize.canonicalize(urls, cache, session=session)
    newly_resolved = {title: cache[title] for title in set(cache) - known_before}
    if newly_resolved:
        storage.update_redirect_cache(conn, newly_resolved)

    # Group visit timestamps by canonical title.
    groups: dict[str, dict] = {}
    dropped_visits = 0
    for url, stamp in valid:
        canonical = mapping.get(url)
        if canonical is None:
            dropped_visits += 1
            continue
        group = groups.setdefault(
            canonical,
            {"timestamps": [], "raw_title": None, "raw_title_ts": None},
        )
        group["timestamps"].append(stamp)
        # raw_title: pre-redirect title from the earliest visit's URL
        if group["raw_title_ts"] is None or stamp < group["raw_title_ts"]:
            group["raw_title"] = normalize.url_to_title(url)
            group["raw_title_ts"] = stamp

    pages_before = storage.count_pages(conn)
    attempted = 0
    inserted = 0
    for canonical, group in groups.items():
        page_id = storage.upsert_page(
            conn, canonical, group["raw_title"], min(group["timestamps"])
        )
        attempted += len(group["timestamps"])
        inserted += storage.insert_visits(conn, page_id, group["timestamps"])
    storage.refresh_visit_counts(conn)
    conn.commit()

    dropped_urls = sorted({url for url in set(urls) if mapping.get(url) is None})
    for url in dropped_urls[:MAX_LOGGED_DROPS]:
        log.info("dropped (not an article or unresolvable): %s", url)
    if len(dropped_urls) > MAX_LOGGED_DROPS:
        log.info("... and %d more dropped URLs", len(dropped_urls) - MAX_LOGGED_DROPS)

    return {
        "visits_in_file": len(rows),
        "malformed_rows": malformed,
        "visits_dropped_non_article": dropped_visits,
        "visits_inserted": inserted,
        "visits_already_present": attempted - inserted,
        "pages_new": storage.count_pages(conn) - pages_before,
        "pages_total": storage.count_pages(conn),
        "visits_total": storage.count_visits(conn),
        "dropped_url_samples": dropped_urls[:MAX_LOGGED_DROPS],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Ingest a standardized history JSON into the app database."
    )
    parser.add_argument("input", type=Path, help="Extractor output JSON")
    parser.add_argument(
        "--db",
        type=Path,
        default=config.DB_PATH,
        help="Database path (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = storage.connect(args.db)
    try:
        report = ingest_file(args.input, conn)
    finally:
        conn.close()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
