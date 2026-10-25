"""Copy repo-local user data into the per-user data directory (opt-in).

Before the app moved its writable files out of the program folder, they
lived in the repo checkout: data/app.db, data/visits.jsonl,
data/sync_state.json, config.local.toml, tokens/, and client_secret*.json.
This module copies them to the same relative paths under
config.USER_DATA_DIR:

    python -m app.migrate --dry-run     # show what would happen
    python -m app.migrate               # do it
    python -m app.migrate --dest DIR    # copy somewhere else (a scratch dir)

Why so careful: data/visits.jsonl is irreplaceable. Chrome expires local
history after about 90 days, so the log holds visits that exist nowhere
else (D9). Hence these rules, every one of which a test pins down:

- COPY, NEVER MOVE. Nothing here deletes, renames, or writes to a source.
  Sources are hashed before and after, and the report says so.
- NEVER OVERWRITE. A destination that already exists and differs is
  reported as a conflict and left alone, because it may hold newer data.
  An identical destination is "already copied", so re-running is safe.
- ONE EXCEPTION, THE LOG. If both logs exist and differ (the app ran
  against the new location before migrating), the source's records are
  APPENDED to the destination with visitlog.append, whose exact dedup means
  neither side loses a record. Appending is the only operation the log
  ever allows, so this is the same thing a sync would do.
- VERIFY EVERY COPY. Plain files by SHA-256. The database by SQLite's
  integrity_check plus a hash of its full SQL dump, because the backup API
  (used so a live database is copied consistently) does not promise a
  byte-identical file.
- NOTHING AUTOMATIC. The app never calls this. It only warns, via
  legacy_status(), that unmigrated data exists.

The reference space under data/reference/ is not user data (it is a
read-only app resource) and is never copied.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import config
from app import visitlog

# Written into the destination after a migration, so the "not migrated yet"
# warning can tell a migrated checkout from an unmigrated one with two
# stat() calls instead of reading both logs on every /health request.
MARKER_NAME = "migrated_from.json"

LOG_RELPATH = Path("data") / "visits.jsonl"
DB_RELPATH = Path("data") / "app.db"

# Left behind by SQLite next to a database. The backup API already reads
# through them, so copying them as files would be redundant at best.
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


@dataclass
class Item:
    """One source file and what the migration does with it."""

    relpath: Path
    kind: str                   # "file" | "sqlite" | "log"
    source: Path
    dest: Path
    action: str = ""            # copy | identical | merge | conflict | error
    detail: str = ""
    verified: bool | None = None
    source_hash_before: str = ""
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_sources(src_root: Path) -> list[tuple[Path, str]]:
    """(relative path, kind) for every legacy user-data file under src_root."""
    found: list[tuple[Path, str]] = []

    data = src_root / "data"
    if data.is_dir():
        for path in sorted(data.rglob("*")):
            rel = path.relative_to(src_root)
            if not path.is_file():
                continue
            if rel.parts[:2] == ("data", "reference"):
                continue  # an app resource, not user data
            if path.name == ".gitkeep":
                continue
            if any(path.name.endswith(s) for s in SQLITE_SIDECARS):
                continue
            if rel == DB_RELPATH:
                found.append((rel, "sqlite"))
            elif rel == LOG_RELPATH:
                found.append((rel, "log"))
            else:
                found.append((rel, "file"))

    config_file = src_root / "config.local.toml"
    if config_file.is_file():
        found.append((Path("config.local.toml"), "file"))

    tokens = src_root / "tokens"
    if tokens.is_dir():
        for path in sorted(tokens.rglob("*")):
            if path.is_file():
                found.append((path.relative_to(src_root), "file"))

    for path in sorted(src_root.glob(config.CLIENT_SECRET_GLOB)):
        if path.is_file():
            found.append((path.relative_to(src_root), "file"))
    return found


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open a database without any possibility of writing to it.

    mode=ro needs SQLite's URI syntax, so the path is percent-encoded: a
    "?" or "#" in a folder name would otherwise be read as URI syntax.
    """
    return sqlite3.connect(
        f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True
    )


def db_fingerprint(path: Path) -> str:
    """Hash of the database's full SQL dump: schema plus every row.

    Two databases with the same fingerprint hold the same data, which is
    what "the copy is correct" has to mean once the backup API is involved.
    """
    digest = hashlib.sha256()
    conn = _open_readonly(path)
    try:
        for line in conn.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
    finally:
        conn.close()
    return digest.hexdigest()


def db_integrity_ok(path: Path) -> bool:
    conn = _open_readonly(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Planning: decide every action before writing anything
# ---------------------------------------------------------------------------

def plan(src_root: Path, dest_root: Path) -> list[Item]:
    items = []
    for rel, kind in find_sources(src_root):
        item = Item(relpath=rel, kind=kind, source=src_root / rel, dest=dest_root / rel)
        try:
            _decide(item)
        except Exception as exc:  # an unreadable file must not hide the rest
            item.action, item.detail = "error", f"could not inspect: {exc}"
        items.append(item)
    return items


def _decide(item: Item) -> None:
    if not item.dest.exists():
        item.action = "copy"
        return
    if item.kind == "sqlite":
        same = db_fingerprint(item.source) == db_fingerprint(item.dest)
    else:
        same = file_sha256(item.source) == file_sha256(item.dest)
    if same:
        item.action, item.detail = "identical", "already copied"
    elif item.kind == "log":
        item.action = "merge"
        item.detail = "both logs exist and differ; source records will be appended"
    else:
        item.action = "conflict"
        item.detail = "destination exists and differs; left untouched"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def migrate(
    src_root: Path | None = None,
    dest_root: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Plan, then (unless dry_run) copy/merge, verify, and report.

    Returns {"items": [...], "sources_unchanged": bool, "ok": bool, ...}.
    ok is False when anything conflicted, failed, or failed verification.
    """
    src_root = Path(src_root or config.PROJECT_ROOT).resolve()
    dest_root = Path(dest_root or config.USER_DATA_DIR).resolve()
    if src_root == dest_root:
        raise ValueError("Source and destination are the same directory.")

    items = plan(src_root, dest_root)
    for item in items:
        if item.action in ("copy", "merge", "identical"):
            item.source_hash_before = file_sha256(item.source)

    if not dry_run:
        for item in items:
            try:
                if item.action == "copy":
                    _copy(item)
                elif item.action == "merge":
                    _merge_log(item)
                elif item.action == "identical":
                    item.verified = True
            except Exception as exc:
                item.action, item.detail = "error", f"failed: {exc}"
                item.verified = False

    # Proof, not promise: every source still hashes the same as before.
    sources_unchanged = all(
        file_sha256(item.source) == item.source_hash_before
        for item in items
        if item.source_hash_before
    )

    problems = [i for i in items if i.action in ("conflict", "error")]
    unverified = [i for i in items if i.verified is False]
    ok = not problems and not unverified and sources_unchanged

    if not dry_run and ok and items:
        _write_marker(src_root, dest_root)

    return {
        "source": str(src_root),
        "destination": str(dest_root),
        "dry_run": dry_run,
        "items": items,
        "sources_unchanged": sources_unchanged,
        "ok": ok,
    }


def _copy(item: Item) -> None:
    """Copy to a temp name, verify it, then rename into place.

    The rename is atomic, so a crash mid-copy leaves either nothing or a
    complete, verified file at the destination - never a torn one.
    """
    item.dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = item.dest.with_name(item.dest.name + ".migrating")
    try:
        if item.kind == "sqlite":
            _sqlite_backup(item.source, tmp)
            item.verified = (
                db_integrity_ok(tmp) and db_fingerprint(tmp) == db_fingerprint(item.source)
            )
        else:
            shutil.copy2(item.source, tmp)   # copy2 keeps the 0600 modes
            item.verified = file_sha256(tmp) == item.source_hash_before
        if not item.verified:
            raise IOError("verification failed; nothing was written")
        os.replace(tmp, item.dest)
        item.detail = "copied and verified"
    finally:
        tmp.unlink(missing_ok=True)


def _sqlite_backup(source: Path, dest: Path) -> None:
    """SQLite's online backup: a consistent snapshot even if the source is
    open elsewhere or has a write-ahead log beside it."""
    src = _open_readonly(source)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    try:
        shutil.copymode(source, dest)
    except OSError:
        pass


def _merge_log(item: Item) -> None:
    """Append the source log's records to the destination log.

    visitlog.append skips any record whose exact dedup key is already
    present, so this is a union: every record from both files survives,
    and running it twice adds nothing the second time.
    """
    before = len(visitlog.existing_keys(item.dest))
    added = visitlog.append(visitlog.iter_records(item.source), path=item.dest)
    wanted = visitlog.existing_keys(item.source)
    have = visitlog.existing_keys(item.dest)
    item.verified = wanted <= have
    item.extra = {"records_before": before, "records_added": added,
                  "records_after": len(have)}
    if not item.verified:
        raise IOError("merge verification failed: source records missing")
    item.detail = (
        f"merged: {added} record(s) appended to the {before} already there"
    )


def _write_marker(src_root: Path, dest_root: Path) -> None:
    """Record what was migrated, for legacy_status()."""
    log = src_root / LOG_RELPATH
    marker = {
        "migrated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(src_root),
        "log_size": log.stat().st_size if log.exists() else None,
        "log_mtime_ns": log.stat().st_mtime_ns if log.exists() else None,
    }
    path = dest_root / "data" / MARKER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# The "you have not migrated" safety net
# ---------------------------------------------------------------------------

def legacy_status(
    src_root: Path | None = None, dest_root: Path | None = None
) -> dict | None:
    """Describe unmigrated repo-local data, or None if there is none.

    Cheap on purpose (a few stat() calls), because GET /health calls it.
    The risk it guards against is a silent split: the app starts a fresh log
    in the new location while the irreplaceable old one sits in the repo.
    The legacy log counts as migrated when the marker matches its current
    size and mtime; if it has changed since (old code ran against the repo
    again), the warning comes back so the new records can be merged.
    """
    src_root = Path(src_root or config.PROJECT_ROOT)
    dest_root = Path(dest_root or config.USER_DATA_DIR)
    if src_root.resolve() == dest_root.resolve():
        return None
    log = src_root / LOG_RELPATH
    if not log.exists():
        return None
    marker_path = dest_root / "data" / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        marker = None
    stat = log.stat()
    if marker and marker.get("log_size") == stat.st_size and (
        marker.get("log_mtime_ns") == stat.st_mtime_ns
    ):
        return None
    return {
        "legacy_log": str(log),
        "message": (
            f"Visit history from before the move to {dest_root} is still in "
            f"{src_root} and has not been copied. Run: python -m app.migrate"
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(report: dict) -> None:
    print(f"Source:      {report['source']}")
    print(f"Destination: {report['destination']}")
    if report["dry_run"]:
        print("DRY RUN: nothing was written.\n")
    else:
        print()
    items = report["items"]
    if not items:
        print("No repo-local user data found; nothing to migrate.")
        return
    width = max(len(str(i.relpath)) for i in items)
    for item in items:
        flag = {True: "verified", False: "NOT VERIFIED", None: ""}[item.verified]
        print(f"  {str(item.relpath):<{width}}  {item.action:<9}  {item.detail}"
              + (f"  [{flag}]" if flag else ""))
    print()
    print("Sources unchanged: "
          + ("yes (re-hashed after the run)" if report["sources_unchanged"] else "NO"))
    conflicts = [i for i in items if i.action == "conflict"]
    if conflicts:
        print(
            "\nConflicts were left untouched. Compare each pair by hand and "
            "remove or rename the destination copy if the repo copy should "
            "win, then re-run."
        )
    if not report["dry_run"] and report["ok"]:
        print(
            "\nDone. The repo copies were not touched. Keep them as a backup "
            "until you have checked the app against the new location; "
            "deleting them is up to you."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy repo-local user data (data/, config.local.toml, "
        "tokens/, client_secret*.json) into the per-user data directory. "
        "Copies only; never moves, deletes, or overwrites."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen and write nothing")
    parser.add_argument("--source", type=Path, default=None,
                        help=f"repo checkout to copy from (default {config.PROJECT_ROOT})")
    parser.add_argument("--dest", type=Path, default=None,
                        help=f"user-data dir to copy into (default {config.USER_DATA_DIR})")
    args = parser.parse_args(argv)

    try:
        report = migrate(args.source, args.dest, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
        return 2
    _print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
