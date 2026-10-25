"""Bring topics, embeddings, and UMAP coordinates up to date with the
pages already in the database.

The same three steps POST /enrich runs, as a standalone process so the
launcher can call it before uvicorn starts (matching how app.sync runs)
and so an enrichment that hangs cannot take the dashboard down with it.

Ingestion does not enrich: /ingest adds pages, and topics/embeddings are
fetched afterwards. A bulk import - a first Google Takeout archive, say -
therefore leaves most of the history with no topic and no coordinates,
which shows up only as a dominant "Unclassified" bar and a near-empty map.
Neither looks like a missing step, so the launcher runs this pass rather
than leaving it to be noticed.

Usage:
    python scripts/enrich_pending.py            # enrich, reporting progress
    python scripts/enrich_pending.py --check    # report the gap, change nothing
    python scripts/enrich_pending.py --quiet    # no progress bars

Every step skips pages that already have a cache row, so a run with
nothing to do costs one query per table and re-running is always safe.
--check exits 0 when nothing is pending and 1 when something is, so a
shell can branch on it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Standalone script: make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from app import embeddings, storage, topics, umap_space


def describe(gaps: dict) -> str:
    parts = []
    if gaps["topics_missing"]:
        parts.append(f"{gaps['topics_missing']} without a topic")
    if gaps["coords_missing"]:
        parts.append(f"{gaps['coords_missing']} without map coordinates")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="report what is pending and exit 1 if anything is",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress progress bars",
    )
    args = parser.parse_args(argv)

    conn = storage.connect(config.DB_PATH)
    try:
        gaps = storage.count_enrichment_gaps(conn)
        pending = gaps["topics_missing"] or gaps["coords_missing"]

        if args.check:
            if pending:
                print(f"Enrichment pending: {describe(gaps)}.")
            return 1 if pending else 0

        if not pending:
            print("Enrichment is up to date.")
            return 0

        print(f"Enriching {describe(gaps)}. This calls the Wikimedia APIs "
              "once per page and can take a while.")
        progress = not args.quiet

        report = topics.label_pages(conn, progress=progress)
        print(f"topics: {report['pages_labeled']} labeled, "
              f"{report['pages_unclassified']} unclassified by the model")

        report = embeddings.embed_pages(conn, progress=progress)
        print(f"embeddings: {report['pages_embedded']} embedded, "
              f"{report['pages_empty_summary']} with no summary text")

        try:
            report = umap_space.project_pages(conn, config.REFERENCE_DIR)
            print(f"projection: {report}")
        except FileNotFoundError as exc:
            # No reference space built yet. Topics still landed, and those
            # are what the category chart needs, so this is not a failure.
            print(f"projection skipped: {exc}", file=sys.stderr)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
