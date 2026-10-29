#!/usr/bin/env python3
"""Standalone extractor: browser history -> standardized JSON (repo_spec S5, D1).

The only code that ever touches browser-specific formats (repo_spec D1), so
the Drive/Takeout sync layer hands its downloads here rather than parsing
them itself. Three input modes, auto-detected by file magic:

- Google Takeout .zip: the Chrome history JSON is located inside and read
  directly out of the archive; no other member is ever decompressed, and
  the archive is never unpacked to disk.

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
import zipfile
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
ZIP_MAGIC = b"PK\x03\x04"

# Inside a Takeout archive the Chrome history file has moved around between
# export versions and is localized ("Chrome" vs "Chrome"/"Navegador Chrome"),
# so members are matched on the basename rather than a fixed full path.
TAKEOUT_HISTORY_BASENAMES = ("history.json", "browserhistory.json")


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


def usec_to_unix(value) -> float | None:
    """Microseconds since either 1601 or 1970 -> Unix seconds.

    A real Google Takeout archive (verified 2026-08-09) writes time_usec as
    microseconds since the UNIX epoch, as a STRING:

        {"url": ..., "time_usec": "1786246804736335"}   -> 2026-08-09

    This contradicts repo_spec S5 and the old reference scripts, which both
    describe time_usec as the Chromium 1601 epoch. Reading a real archive
    with the 1601 assumption subtracts 11644473600 and lands in 1663 BC, so
    every row fails the plausibility check and is dropped: 2804 Wikipedia
    visits silently became zero. The SQLite path is unaffected, because
    visits.visit_time genuinely is the 1601 epoch.

    Rather than swapping one hardcoded assumption for another, both readings
    are computed and the plausible one wins. They cannot both be plausible:
    a value that is a sane date under the 1601 epoch is ~1.3e16, which read
    as Unix microseconds lands in the year 2400+, well past MAX_UNIX_S.
    """
    try:
        micros = float(value)
    except (TypeError, ValueError):
        return None
    as_unix = micros / 1_000_000
    as_chromium = as_unix - CHROME_EPOCH_OFFSET_S
    if MIN_UNIX_S <= as_chromium <= MAX_UNIX_S:
        return as_chromium
    if MIN_UNIX_S <= as_unix <= MAX_UNIX_S:
        return as_unix
    return None


def json_row_to_unix(row: dict) -> float | None:
    """Pick whichever timestamp field a JSON export row carries.

    visitTime is preferred over lastVisitTime: export-extension rows carry
    both, and only visitTime is per-visit.
    """
    if row.get("time_usec") is not None:
        return usec_to_unix(row["time_usec"])
    for key in ("visitTime", "lastVisitTime"):
        if row.get(key) is not None:
            return epoch_ms_to_unix(row[key])
    return None


# ---------------------------------------------------------------------------
# Input readers -> raw (url, title, unix_seconds) rows
# ---------------------------------------------------------------------------

def load_json_export(path: Path) -> list[tuple[str, str, float | None]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        return parse_history_payload(data, origin=str(path))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def parse_history_payload(
    data, origin: str = "input"
) -> list[tuple[str, str, float | None]]:
    """Rows from an already-parsed Chrome/Takeout history JSON payload.

    Accepts the Takeout wrapper {"Browser History": [...]} or a bare list.
    Raises ValueError with a description of what was actually found rather
    than returning an empty list, so a changed export format surfaces as a
    loud failure instead of a silent zero-visit sync.
    """
    if isinstance(data, dict):
        rows = data.get("Browser History")
        if rows is None:  # tolerate a single differently-named array
            arrays = [v for v in data.values() if isinstance(v, list)]
            rows = arrays[0] if len(arrays) == 1 else None
        if rows is None:
            raise ValueError(
                f"{origin}: JSON object has no 'Browser History' key and no "
                f"single array to fall back to (keys: {sorted(data)[:10]}). "
                "The Takeout export format may have changed."
            )
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError(
            f"{origin}: expected a JSON object or list, got {type(data).__name__}."
        )

    dicts = [row for row in rows if isinstance(row, dict)]
    if rows and not dicts:
        raise ValueError(
            f"{origin}: history array holds no objects "
            f"(first element is {type(rows[0]).__name__})."
        )
    if dicts and not any(row.get("url") for row in dicts):
        raise ValueError(
            f"{origin}: no row carries a 'url' field "
            f"(keys seen: {sorted(dicts[0])[:10]}). Format may have changed."
        )
    return [
        (row.get("url") or "", row.get("title") or "", json_row_to_unix(row))
        for row in dicts
    ]


class NoHistoryInArchive(ValueError):
    """The archive contains no Chrome history file.

    Distinct from a malformed one on purpose. Takeout splits a large export
    across several numbered zips, and only one part carries History.json;
    the others hold Calendar, Bookmarks, and so on. Treating those as errors
    would warn on every sync forever. A history file that is PRESENT but
    unreadable is a real error and still raises plain ValueError.
    """


def find_takeout_history_member(archive: zipfile.ZipFile) -> str:
    """Name of the Chrome history JSON inside a Takeout archive."""
    candidates = [
        name
        for name in archive.namelist()
        if not name.endswith("/")
        and Path(name).name.lower() in TAKEOUT_HISTORY_BASENAMES
    ]
    if not candidates:
        # Be forgiving about the basename if the path clearly says Chrome.
        candidates = [
            name
            for name in archive.namelist()
            if name.lower().endswith(".json") and "chrome" in name.lower()
        ]
    if not candidates:
        json_members = [n for n in archive.namelist() if n.lower().endswith(".json")]
        raise NoHistoryInArchive(
            "No Chrome history JSON found in the archive. Looked for "
            f"{list(TAKEOUT_HISTORY_BASENAMES)} and any Chrome/*.json; the "
            f"archive holds {len(archive.namelist())} members "
            f"({len(json_members)} JSON). This is normal for one part of a "
            "multi-part export; if NO part has history, re-run Takeout with "
            "Chrome > History selected."
        )
    # Shortest path wins: prefers Takeout/Chrome/History.json over anything
    # nested deeper, and is deterministic when several match.
    return min(candidates, key=lambda name: (len(name), name))


def load_takeout_zip(source) -> list[tuple[str, str, float | None]]:
    """Rows from a Takeout .zip, given a path or a binary file object.

    Only the single history member is read; the rest of the archive is never
    decompressed. Callers filter to Wikipedia immediately afterwards
    (build_visits) so nothing else is ever persisted.
    """
    with zipfile.ZipFile(source) as archive:
        member = find_takeout_history_member(archive)
        with archive.open(member) as handle:
            raw = handle.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{member}: not readable as UTF-8 JSON ({exc}).") from exc
    return parse_history_payload(data, origin=member)


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


def chrome_base_dir() -> Path:
    """Root of Chrome's user-data directory for this OS."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise SystemExit("LOCALAPPDATA is not set; pass --input explicitly.")
        return Path(local) / "Google/Chrome/User Data"
    return Path.home() / ".config/google-chrome"


def chrome_profile_names() -> dict[str, dict]:
    """Directory name -> {display_name, account} from Chrome's Local State.

    Chrome stores the human-facing profile name ("Work", "Joseph") and the
    signed-in address in a JSON index next to the profile directories. The
    directory names ("Default", "Profile 1") are the stable identifiers, but
    they are meaningless to a person choosing which profile to read, so
    setup shows both. Returns {} if the file is missing or unreadable.
    """
    try:
        state = chrome_base_dir() / "Local State"
    except SystemExit:
        return {}
    if not state.exists():
        return {}
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    cache = (data.get("profile") or {}).get("info_cache") or {}
    return {
        directory: {
            "display_name": (info or {}).get("name") or directory,
            "account": (info or {}).get("user_name") or "",
        }
        for directory, info in cache.items()
        if isinstance(info, dict)
    }


def find_chrome_profiles(only: list[str] | None = None) -> list[dict]:
    """Chrome profiles on this machine that have a History DB.

    Each entry: {directory, display_name, account, path}. "Default" first,
    then "Profile N" in numeric order. "System Profile" is excluded; it never
    holds browsing.

    `only` restricts the result to the named profiles, matched against the
    directory name first and the display name second, both case-insensitively.
    Passing None or an empty list means every profile: the signed-in profile
    is often not "Default", so defaulting to one profile would silently
    capture zero visits on a machine whose browsing lives in "Profile 1".
    """
    try:
        base = chrome_base_dir()
    except SystemExit:
        return []
    if not base.exists():
        return []

    names = chrome_profile_names()
    found = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name == "System Profile":
            continue
        history = entry / "History"
        if not history.exists():
            continue
        info = names.get(entry.name, {})
        found.append({
            "directory": entry.name,
            "display_name": info.get("display_name") or entry.name,
            "account": info.get("account") or "",
            "path": history,
        })

    if only:
        wanted = {str(name).strip().lower() for name in only if str(name).strip()}
        found = [
            profile for profile in found
            if profile["directory"].lower() in wanted
            or profile["display_name"].lower() in wanted
        ]

    def order(profile: dict):
        name = profile["directory"]
        if name == "Default":
            return (0, 0, name)
        if name.startswith("Profile "):
            try:
                return (1, int(name.split(" ", 1)[1]), name)
            except ValueError:
                pass
        return (2, 0, name)

    return sorted(found, key=order)


def find_chrome_history(profile: str) -> Path:
    """Default Chrome History DB location for this OS."""
    path = chrome_base_dir() / profile / "History"
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
        "standardized JSON that wikiwrapped ingests."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Takeout .zip, Chrome History SQLite DB, or JSON export. "
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
        magic = handle.read(max(len(SQLITE_MAGIC), len(ZIP_MAGIC)))
    if magic.startswith(SQLITE_MAGIC):
        mode, rows = "Chrome DB", load_chrome_db(source)
    elif magic.startswith(ZIP_MAGIC):
        mode = "Takeout archive"
        try:
            rows = load_takeout_zip(source)
        except (ValueError, zipfile.BadZipFile) as exc:
            raise SystemExit(f"{source}: {exc}")
    else:
        mode, rows = "JSON export", load_json_export(source)
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
