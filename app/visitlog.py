"""Append-only JSONL log of raw visits: the durable source of truth (D9).

data/app.db is a derived artifact and can be deleted and rebuilt from this
file at any time. This file cannot be rebuilt from anything, because Chrome
expires local history after about 90 days: whatever was captured here before
the window rolled is the only remaining copy. It is the thing worth backing
up.

One JSON object per line:

    {"url": ..., "title": ..., "timestamp": ..., "source": ..., "account": ...}

- timestamp: UTC ISO-8601, second precision, matching the extractor's output.
- source:    "chrome_local" | "takeout" (SOURCES below).
- account:   the Google account this came from, or null when unknown (the
             local Chrome reader cannot tell which account a profile is
             signed into without reading Chrome's preferences).

Never rewritten in place; new visits are appended and the file only grows.

Dedup here is EXACT on (url, timestamp, source, account), which is narrower
than it first looks: when Takeout and the local Chrome DB both report one
physical visit, both records are kept. That is deliberate. Those two rows are
the evidence that two mechanisms saw the same event, which is what
visit_sources records. Collapsing them into a single visit is a separate
step (app/dedup.py) that runs when the DB is built, because it needs
normalized post-redirect URLs and therefore the redirect cache.

So: this module answers "has this exact record already been logged", and
app/dedup.py answers "are these two records the same physical visit".
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

import config

SOURCE_CHROME_LOCAL = "chrome_local"
SOURCE_TAKEOUT = "takeout"
# Reconstructed from app.db for visits that predate this log. Their raw URL
# was never recorded, so it is synthesized from the canonical title; kept as
# its own source so that reconstruction is never mistaken for a real capture.
SOURCE_LEGACY_DB = "legacy_db"
SOURCES = (SOURCE_CHROME_LOCAL, SOURCE_TAKEOUT, SOURCE_LEGACY_DB)

FIELDS = ("url", "title", "timestamp", "source", "account")


def record(
    url: str,
    title: str | None,
    timestamp: str,
    source: str,
    account: str | None = None,
) -> dict:
    """Build one log record, normalizing the optional fields."""
    if source not in SOURCES:
        raise ValueError(f"Unknown source {source!r}; expected one of {SOURCES}.")
    if not url or not timestamp:
        raise ValueError("A visit record needs both a url and a timestamp.")
    return {
        "url": url,
        "title": title or "",
        "timestamp": timestamp,
        "source": source,
        "account": account or None,
    }


def dedup_key(entry: dict) -> tuple:
    return (
        entry.get("url"),
        entry.get("timestamp"),
        entry.get("source"),
        entry.get("account"),
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def iter_records(path: str | Path | None = None) -> Iterator[dict]:
    """Yield every well-formed record in file order, including duplicates.

    A truncated final line (an append interrupted by a crash) is skipped
    rather than raising: losing one visit is strictly better than making the
    whole log unreadable.
    """
    path = Path(path or config.VISIT_LOG_PATH)
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("url") and entry.get("timestamp"):
                yield entry


def read(path: str | Path | None = None) -> list[dict]:
    """Every record, deduped exactly, in first-seen order."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for entry in iter_records(path):
        key = dedup_key(entry)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def existing_keys(path: str | Path | None = None) -> set[tuple]:
    """Dedup keys already on disk, so append() can skip re-logging them."""
    return {dedup_key(entry) for entry in iter_records(path)}


def latest_timestamp(
    source: str | None = None,
    account: str | None = None,
    path: str | Path | None = None,
) -> str | None:
    """Newest timestamp logged, optionally scoped to one source/account.

    ISO-8601 UTC strings compare lexicographically in time order, so max()
    is chronological. Used to make the local Chrome pass incremental.
    """
    newest = None
    for entry in iter_records(path):
        if source is not None and entry.get("source") != source:
            continue
        if account is not None and entry.get("account") != account:
            continue
        stamp = entry.get("timestamp")
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    return newest


def stats(path: str | Path | None = None) -> dict:
    """Counts for the sync report and /health."""
    total = 0
    by_source: dict[str, int] = {}
    accounts: set[str] = set()
    earliest = latest = None
    for entry in iter_records(path):
        total += 1
        source = entry.get("source") or "unknown"
        by_source[source] = by_source.get(source, 0) + 1
        if entry.get("account"):
            accounts.add(entry["account"])
        stamp = entry.get("timestamp")
        if stamp:
            if earliest is None or stamp < earliest:
                earliest = stamp
            if latest is None or stamp > latest:
                latest = stamp
    return {
        "records": total,
        "by_source": by_source,
        "accounts": sorted(accounts),
        "earliest": earliest,
        "latest": latest,
    }


# ---------------------------------------------------------------------------
# Writing (append-only)
# ---------------------------------------------------------------------------

def append(
    records: Iterable[dict],
    path: str | Path | None = None,
    known: set[tuple] | None = None,
) -> int:
    """Append records not already present; returns how many were written.

    Pass `known` (from existing_keys) to avoid re-reading the log when
    appending several batches in one sync pass. It is updated in place.

    The write is one open/write/flush/fsync: a sync that dies partway leaves
    a prefix of valid lines plus at most one torn line, which iter_records
    skips.
    """
    path = Path(path or config.VISIT_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if known is None:
        known = existing_keys(path)

    fresh = []
    for entry in records:
        key = dedup_key(entry)
        if key in known:
            continue
        known.add(key)
        fresh.append(entry)
    if not fresh:
        return 0

    with open(path, "a", encoding="utf-8") as handle:
        for entry in fresh:
            handle.write(
                json.dumps(
                    {field: entry.get(field) for field in FIELDS},
                    ensure_ascii=True,
                    sort_keys=True,
                )
                + "\n"
            )
        handle.flush()
        os.fsync(handle.fileno())
    return len(fresh)


def compact(path: str | Path | None = None) -> dict:
    """Drop records superseded by a more specific duplicate of themselves.

    One case, which arises when a record gains a field it did not have when
    it was first logged: local Chrome visits were originally logged with no
    account, and later gained one (read from Chrome's Local State). Because
    the dedup key includes the account, the same visit is then logged twice,
    differing only in that field.

    For each (url, timestamp, source), if any record names an account, the
    account-less ones are dropped as the less specific version of the same
    fact. Two DIFFERENT accounts for one visit are both kept: that is two
    accounts genuinely seeing it, not a duplicate.

    Rewrites the file, so it is a migration step and deliberately not part of
    the sync path, which only ever appends. The database is unaffected either
    way, since dedup already collapses these before they reach it.
    """
    entries = read(path)
    groups: dict[tuple, list[dict]] = {}
    for entry in entries:
        key = (entry.get("url"), entry.get("timestamp"), entry.get("source"))
        groups.setdefault(key, []).append(entry)

    kept: list[dict] = []
    for key in groups:
        candidates = groups[key]
        accounted = [e for e in candidates if e.get("account")]
        chosen = accounted or candidates
        # Prefer a record that also carries a title over a bare one.
        titled = [e for e in chosen if e.get("title")]
        kept.extend(titled or chosen)

    kept.sort(key=lambda entry: (entry["timestamp"], entry["url"]))
    removed = len(entries) - len(kept)
    if removed:
        rewrite(kept, path)
    return {"before": len(entries), "after": len(kept), "removed": removed}


def rewrite(records: Iterable[dict], path: str | Path | None = None) -> int:
    """Replace the log wholesale, via a temp file and an atomic rename.

    Deliberately NOT part of the sync path, which only ever appends. This
    exists for one-off migrations (importing a pre-log export) and for tests.
    Callers own the decision to discard what is there.
    """
    path = Path(path or config.VISIT_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    written = 0
    try:
        with handle:
            for entry in records:
                handle.write(
                    json.dumps(
                        {field: entry.get(field) for field in FIELDS},
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                    + "\n"
                )
                written += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return written


# ---------------------------------------------------------------------------
# Handing off to ingest
# ---------------------------------------------------------------------------

def to_document(records: Iterable[dict]) -> dict:
    """Wrap log records in the standardized schema app.ingest consumes (S5).

    The log is a superset of that schema: it carries source and account too,
    and ingest reads them to populate visit_sources.
    """
    import datetime as dt

    visits = [
        {
            "url": entry["url"],
            "title": entry.get("title") or "",
            "timestamp": entry["timestamp"],
            "source": entry.get("source"),
            "account": entry.get("account"),
        }
        for entry in records
    ]
    visits.sort(key=lambda visit: visit["timestamp"])
    return {
        "schema_version": config.SCHEMA_VERSION,
        "source_browser": "chrome",
        "exported_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "visits": visits,
    }
