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
from app import dedup, normalize, storage

log = logging.getLogger(__name__)

MAX_LOGGED_DROPS = 20


def ingest_file(
    path: str | Path,
    conn: sqlite3.Connection,
    session=None,
    default_source: str | None = None,
    default_account: str | None = None,
) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return ingest_document(
        document,
        conn,
        session=session,
        default_source=default_source,
        default_account=default_account,
    )


def ingest_document(
    document: dict,
    conn: sqlite3.Connection,
    session=None,
    default_source: str | None = None,
    default_account: str | None = None,
) -> dict:
    """Ingest one standardized export; returns a report of what happened.

    Visits may carry per-row "source" and "account" fields (the append-only
    log emits them); default_source/default_account fill in for rows that do
    not, so a plain schema-1.0 export from the extractor still ingests
    unchanged and simply records no provenance.
    """
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version != config.SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported schema_version {version!r}; "
            f"expected {config.SCHEMA_VERSION!r}. Re-run the extractor."
        )

    rows = document.get("visits", [])
    valid = [
        (
            row["url"],
            row["timestamp"],
            row.get("source") or default_source,
            row.get("account") or default_account,
        )
        for row in rows
        if isinstance(row, dict) and row.get("url") and row.get("timestamp")
    ]
    malformed = len(rows) - len(valid)

    # Resolve URL -> canonical title, network only for cache misses (D6).
    cache = storage.get_redirect_cache(conn)
    known_before = set(cache)
    urls = [url for url, _, _, _ in valid]
    mapping = normalize.canonicalize(urls, cache, session=session)
    newly_resolved = {title: cache[title] for title in set(cache) - known_before}
    if newly_resolved:
        storage.update_redirect_cache(conn, newly_resolved)

    # Resolved rows become dedup entries keyed on the CANONICAL title, so a
    # desktop URL, its mobile form, and a redirect source and target are all
    # one page before any timestamp comparison happens (D5, D11).
    entries: list[dict] = []
    raw_titles: dict[str, dict] = {}
    dropped_visits = 0
    for url, stamp, source, account in valid:
        canonical = mapping.get(url)
        if canonical is None:
            dropped_visits += 1
            continue
        entries.append(
            {
                "key": canonical,
                "timestamp": stamp,
                "source": source,
                "account": account,
            }
        )
        # raw_title: pre-redirect title from the earliest visit's URL
        seen = raw_titles.setdefault(canonical, {"title": None, "stamp": None})
        if seen["stamp"] is None or stamp < seen["stamp"]:
            seen["title"] = normalize.url_to_title(url)
            seen["stamp"] = stamp

    # Collapse records describing the same physical visit, merging their
    # provenance. With the tolerance at 0 this is an exact same-second match,
    # which is what both input paths already produce (see app/dedup.py).
    collapsed = dedup.collapse(entries)
    groups: dict[str, dict] = {}
    for visit in collapsed:
        group = groups.setdefault(
            visit["key"], {"timestamps": [], "provenance": {}}
        )
        group["timestamps"].append(visit["timestamp"])
        if visit["sources"]:
            group["provenance"][visit["timestamp"]] = visit["sources"]

    pages_before = storage.count_pages(conn)
    # attempted counts RAW resolved rows, not collapsed visits, so
    # visits_already_present keeps meaning "input rows that produced no new
    # visit row" whether the cause was dedup or a prior ingest.
    attempted = len(entries)
    inserted = 0
    source_rows = 0
    for canonical, group in groups.items():
        page_id = storage.upsert_page(
            conn, canonical, raw_titles[canonical]["title"], min(group["timestamps"])
        )
        inserted += storage.insert_visits(conn, page_id, group["timestamps"])
        source_rows += storage.attach_visit_sources(
            conn, page_id, group["provenance"]
        )
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
        # Rows this ingest merged into an existing collapsed visit, i.e. the
        # cross-source dedup that actually happened within this document.
        "visits_deduplicated": attempted - len(collapsed),
        "visit_source_rows": source_rows,
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
