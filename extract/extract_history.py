#!/usr/bin/env python3
"""Standalone extractor: browser history -> standardized JSON (repo_spec S5, D1).

The only code that ever touches browser-specific formats. Two input modes,
auto-detected by file magic:

- Chrome History SQLite DB: auto-located per-OS (or --input PATH), copied to
  a temp file first because Chrome locks it while running. Reads per-visit
  rows from the visits table joined to urls (D2: one row per visit, NOT the
  collapsed last_visit_time per URL). Timestamps are microseconds since
  1601-01-01.
- JSON export (Google Takeout or history-export extensions): either the
  Takeout wrapper {"Browser History": [...]} or a bare list. Timestamps are
  time_usec (microseconds since 1601) or visitTime/lastVisitTime
  (milliseconds since 1970), detected per row.

Output: Wikipedia visits only, all timestamps normalized to UTC ISO-8601,
in the schema_version 1.0 JSON that app/ingest.py consumes. Nothing
downstream ever needs to know which browser the data came from.

Timestamps are truncated to second precision and rows are deduped on
(url, timestamp). This is deliberate: Chrome logs some navigations twice
within tens of milliseconds (observed with media-viewer clicks), and a
same-URL-same-second pair is that artifact, not two reading events. Any
genuine revisit lands in a later second and is preserved.

Stdlib-only so it runs without the conda env:

    python extract/extract_history.py                        # find Chrome DB
    python extract/extract_history.py --profile "Profile 1"
    python extract/extract_history.py --input export.json --output wiki.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# Must match config.SCHEMA_VERSION (duplicated here to stay standalone).
SCHEMA_VERSION = "1.0"

# Chrome stores times as microseconds since 1601-01-01 (Windows FILETIME
# epoch); this is the offset from that epoch to the Unix epoch, in seconds.
CHROME_EPOCH_OFFSET_S = 11_644_473_600

# Rows whose timestamps fall outside these bounds are corrupt, not real
# browsing history (e.g. zeroed fields decode to 1601 or 1970).
MIN_UNIX_S = 631_152_000     # 1990-01-01
MAX_UNIX_S = 4_102_444_800   # 2100-01-01

SQLITE_MAGIC = b"SQLite format 3\x00"


# ---------------------------------------------------------------------------
# Timestamp conversions
# ---------------------------------------------------------------------------

def chrome_usec_to_unix(value) -> float | None:
    """Microseconds since 1601-01-01 -> Unix seconds."""
    try:
        return float(value) / 1_000_000 - CHROME_EPOCH_OFFSET_S
    except (TypeError, ValueError):
        return None


def epoch_ms_to_unix(value) -> float | None:
    """Milliseconds since 1970-01-01 (floats OK) -> Unix seconds."""
    try:
        return float(value) / 1_000
    except (TypeError, ValueError):
        return None


def to_iso_utc(unix_s: float | None) -> str | None:
    """Unix seconds -> UTC ISO-8601 (second precision), None if implausible."""
    if unix_s is None or not (MIN_UNIX_S <= unix_s <= MAX_UNIX_S):
        return None
    stamp = dt.datetime.fromtimestamp(unix_s, tz=dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def json_row_to_unix(row: dict) -> float | None:
    """Pick whichever timestamp field a JSON export row carries.

    visitTime is preferred over lastVisitTime: export-extension rows carry
    both, and only visitTime is per-visit.
    """
    if row.get("time_usec") is not None:
        return chrome_usec_to_unix(row["time_usec"])
    for key in ("visitTime", "lastVisitTime"):
        if row.get(key) is not None:
            return epoch_ms_to_unix(row[key])
    return None


# ---------------------------------------------------------------------------
# Input readers -> raw (url, title, unix_seconds) rows
# ---------------------------------------------------------------------------

def load_json_export(path: Path) -> list[tuple[str, str, float | None]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("Browser History") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit(
            f"{path}: unrecognized JSON layout; expected a list of visits or "
            'a {"Browser History": [...]} object'
        )
    return [
        (row.get("url") or "", row.get("title") or "", json_row_to_unix(row))
        for row in rows
        if isinstance(row, dict)
    ]


def load_chrome_db(path: Path) -> list[tuple[str, str, float | None]]:
    """Read per-visit rows from a Chrome History DB via a temp copy."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    try:
        shutil.copy2(path, tmp.name)
        conn = sqlite3.connect(tmp.name)
        try:
            rows = conn.execute(
                """
                SELECT urls.url, urls.title, visits.visit_time
                FROM visits JOIN urls ON visits.url = urls.id
                WHERE urls.url LIKE '%wikipedia.org%'
                ORDER BY visits.visit_time
                """
            ).fetchall()
        finally:
            conn.close()
    finally:
        os.unlink(tmp.name)
    return [
        (url or "", title or "", chrome_usec_to_unix(visit_time))
        for url, title, visit_time in rows
    ]


def find_chrome_history(profile: str) -> Path:
    """Default Chrome History DB location for this OS."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library/Application Support/Google/Chrome"
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise SystemExit("LOCALAPPDATA is not set; pass --input explicitly.")
        base = Path(local) / "Google/Chrome/User Data"
    else:
        base = Path.home() / ".config/google-chrome"
    path = base / profile / "History"
    if not path.exists():
        raise SystemExit(
            f"Chrome History DB not found at {path}. Pass --input with the "
            "path to a History DB or a JSON export, or --profile for a "
            'non-"Default" Chrome profile.'
        )
    return path


# ---------------------------------------------------------------------------
# Filtering + assembly
# ---------------------------------------------------------------------------

def is_wikipedia_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "wikipedia.org" or host.endswith(".wikipedia.org")


def build_visits(rows: list[tuple[str, str, float | None]]) -> tuple[list[dict], dict]:
    """Filter to Wikipedia, normalize timestamps, dedupe (url, timestamp)."""
    counts = {"read": len(rows), "non_wikipedia": 0, "bad_timestamp": 0,
              "duplicate": 0, "kept": 0}
    seen = set()
    visits = []
    for url, title, unix_s in rows:
        if not url or not is_wikipedia_url(url):
            counts["non_wikipedia"] += 1
            continue
        stamp = to_iso_utc(unix_s)
        if stamp is None:
            counts["bad_timestamp"] += 1
            continue
        key = (url, stamp)
        if key in seen:
            counts["duplicate"] += 1
            continue
        seen.add(key)
        visits.append({"url": url, "title": title, "timestamp": stamp})
    visits.sort(key=lambda v: v["timestamp"])
    counts["kept"] = len(visits)
    return visits, counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Wikipedia visits from browser history into the "
        "standardized JSON that wikipedia-wrapped ingests."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Chrome History SQLite DB or JSON export (Takeout/extension). "
        "Default: auto-locate this machine's Chrome History DB.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("wikipedia_history.json"),
        help="Where to write the standardized JSON (default: %(default)s).",
    )
    parser.add_argument(
        "--profile",
        default="Default",
        help="Chrome profile directory name when auto-locating "
        "(default: %(default)s).",
    )
    args = parser.parse_args(argv)

    source = args.input or find_chrome_history(args.profile)
    if not source.exists():
        raise SystemExit(f"Input not found: {source}")

    with open(source, "rb") as handle:
        is_sqlite = handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    rows = load_chrome_db(source) if is_sqlite else load_json_export(source)
    visits, counts = build_visits(rows)

    document = {
        "schema_version": SCHEMA_VERSION,
        "source_browser": "chrome",
        "exported_at": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "visits": visits,
    }
    args.output.write_text(
        json.dumps(document, ensure_ascii=True, indent=2), encoding="utf-8"
    )

    mode = "Chrome DB" if is_sqlite else "JSON export"
    print(f"Read {counts['read']} rows from {source} ({mode}).")
    print(
        f"Kept {counts['kept']} Wikipedia visits "
        f"(skipped {counts['non_wikipedia']} non-Wikipedia, "
        f"{counts['bad_timestamp']} bad-timestamp, "
        f"{counts['duplicate']} duplicate rows)."
    )
    if visits:
        print(f"Date range: {visits[0]['timestamp']} to {visits[-1]['timestamp']}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
