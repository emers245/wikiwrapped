"""Check-on-launch sync: refresh the visit log, then the DB (D12).

Not a daemon and not a scheduler. This is a single pass that run.sh invokes
before starting uvicorn, and that you can run by hand any time:

    python -m app.sync                 # sync now (respects the interval)
    python -m app.sync --force         # ignore the interval
    python -m app.sync --on-launch     # what run.sh calls; obeys the config gate
    python -m app.sync --rebuild       # re-ingest the whole log into the DB
    python -m app.sync --no-drive      # local Chrome only

The order matters. Visits are appended to the append-only log FIRST, and the
DB is updated from the log second. The log is the durable artifact: if the
ingest step dies halfway, nothing captured is lost and the next run picks it
up. Chrome expires local history after about 90 days, so a visit that fails
to reach the log may be gone for good.

THE SYNC MUST NEVER BLOCK OR BREAK STARTUP. Every expected failure - Drive
unreachable, token expired, archive malformed, Wikipedia unreachable - is
caught, recorded in the returned report and in sync_state.json (in the
user-data dir), and surfaced by GET /health. run() does not raise for any of them, and the
dashboard serves whatever data already exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import config
from app import dedup, drive, ingest, localconfig, migrate, storage, visitlog
from extract import extract_history

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync state: what makes a relaunch cheap
# ---------------------------------------------------------------------------

def load_state(path: str | Path | None = None) -> dict:
    path = Path(path or config.SYNC_STATE_PATH)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict, path: str | Path | None = None) -> None:
    path = Path(path or config.SYNC_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")
    except OSError as exc:  # a read-only data dir must not break startup
        log.warning("Could not write sync state: %s", exc)


def hours_since_last_sync(state: dict) -> float | None:
    stamp = state.get("last_attempt")
    if not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = dt.datetime.now(dt.timezone.utc) - when
    return delta.total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Source passes: each appends to the log and reports, never raises
# ---------------------------------------------------------------------------

def sync_local_chrome(log_path=None, known=None, profiles=None, only=None) -> dict:
    """Read the selected local Chrome profiles and append their visits.

    `only` is the configured profile filter (local.chrome_profiles); empty or
    None reads every profile found.

    Each profile's whole DB is re-read rather than only rows after the last
    logged timestamp: it is a few hundred rows, and the log's exact dedup
    makes re-reading free. Filtering by timestamp would also miss back-dated
    rows that Chrome Sync pulls in from another device after the fact.
    """
    report = {"profiles": [], "visits_logged": 0, "errors": []}
    if profiles is not None:
        found = profiles
    else:
        found = extract_history.find_chrome_profiles(only=only)
    if not found:
        if only:
            available = ", ".join(
                f"{p['directory']} ({p['display_name']})"
                for p in extract_history.find_chrome_profiles()
            )
            report["errors"].append(
                f"No Chrome profile matched {only}. Available: "
                f"{available or 'none'}. Re-run scripts/setup_takeout.py "
                "to change the selection."
            )
        else:
            report["errors"].append(
                "No local Chrome profile with a History database was found."
            )
        return report

    for profile in found:
        name = profile["directory"]
        try:
            rows = extract_history.load_chrome_db(Path(profile["path"]))
            visits, counts = extract_history.build_visits(rows)
            records = [
                visitlog.record(
                    visit["url"],
                    visit["title"],
                    visit["timestamp"],
                    visitlog.SOURCE_CHROME_LOCAL,
                    # Chrome's Local State names the signed-in account for
                    # each profile, so local visits can be attributed too.
                    account=profile.get("account") or None,
                )
                for visit in visits
            ]
            added = visitlog.append(records, path=log_path, known=known)
            report["visits_logged"] += added
            report["profiles"].append({
                "profile": name,
                "display_name": profile.get("display_name") or name,
                "account": profile.get("account") or "",
                "wikipedia_visits": counts["kept"],
                "new": added,
            })
        except Exception as exc:
            # A locked or corrupt profile must not stop the other profiles.
            report["errors"].append(f"Chrome profile {name!r}: {exc}")
    return report


def import_export(path, source=None, account=None, log_path=None) -> dict:
    """Append a standardized export JSON (or a Takeout zip) to the log.

    The migration path for history captured before the log existed. Anything
    the extractor already wrote to wikipedia_history.json is very likely to
    reach FURTHER BACK than local Chrome still does, because Chrome expires
    history after about 90 days: those visits exist only in that file and in
    the derived database, so importing them is the only way to make them
    durable.

    Idempotent, like every other write to the log.
    """
    path = Path(path)
    document = None
    if path.suffix.lower() == ".zip":
        visits, _counts = extract_history.build_visits(
            extract_history.load_takeout_zip(path)
        )
        source = source or visitlog.SOURCE_TAKEOUT
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        visits = document.get("visits", [])
        source = source or visitlog.SOURCE_CHROME_LOCAL

    records = [
        visitlog.record(
            row["url"],
            row.get("title"),
            row["timestamp"],
            row.get("source") or source,
            account=row.get("account") or account,
        )
        for row in visits
        if isinstance(row, dict) and row.get("url") and row.get("timestamp")
    ]
    added = visitlog.append(records, path=log_path)
    return {"file": str(path), "visits_in_file": len(visits), "new_to_log": added}


def backfill_from_db(conn, log_path=None) -> dict:
    """Copy DB-only visits into the log so the log becomes the complete record.

    Some visits predate the log: they were ingested from an export whose
    source file is gone and whose window local Chrome no longer covers. The
    database is their ONLY copy, and it is supposed to be the disposable,
    rebuildable artifact. This inverts that, so deleting app.db stops being
    a way to lose history.

    The raw URL was never stored (the DB keys on canonical titles), so it is
    reconstructed as /wiki/<canonical title>. That round-trips through
    url_to_title to the same title, which is the only thing downstream joins
    on, but it is lossy: the original query string, fragment, and mobile-vs-
    desktop host are gone. Hence the separate legacy_db source, so a
    reconstruction is never mistaken for a real capture.
    """
    orphans = storage.visits_without_provenance(conn)
    records = []
    for row in orphans:
        title = row["canonical_title"]
        slug = quote(title.replace(" ", "_"), safe="/:()',!$*&+=@")
        records.append(
            visitlog.record(
                f"https://en.wikipedia.org/wiki/{slug}",
                row["raw_title"] or title,
                row["timestamp"],
                visitlog.SOURCE_LEGACY_DB,
                account=None,
            )
        )
    added = visitlog.append(records, path=log_path)
    return {"db_only_visits": len(orphans), "new_to_log": added}


def sync_takeout(conn, settings, log_path=None, known=None) -> dict:
    """Download and ingest any unprocessed Takeout archive, per account.

    Read-only throughout: archives are listed and downloaded, never renamed,
    moved, trashed, or deleted. Retention in Drive is the user's decision.
    """
    report = {
        "accounts": [],
        "archives_processed": 0,
        "archives_skipped": 0,
        "visits_logged": 0,
        "reauthorize": [],
        "errors": [],
    }
    emails = localconfig.account_emails(settings)
    if not emails:
        report["errors"].append(
            "Takeout is enabled but no account is authorized. Run: "
            "python scripts/setup_takeout.py"
        )
        return report

    configured_folder = localconfig.drive_folder_id(settings)
    for email in emails:
        account_report = {
            "account": email, "archives": 0, "visits_logged": 0, "status": "ok"
        }
        try:
            creds = drive.load_credentials(email, settings)
            # The setting may be a folder name or an ID; resolve per account,
            # since a folder ID is only meaningful to the account that sees it.
            folder_id = drive.resolve_folder_id(creds, configured_folder)
            archives = drive.list_takeout_archives(creds, folder_id=folder_id)
        except drive.ReauthorizationNeeded as exc:
            # Expected roughly weekly while the OAuth app is in Testing.
            account_report["status"] = "reauthorize"
            report["reauthorize"].append(email)
            report["errors"].append(str(exc))
            report["accounts"].append(account_report)
            continue
        except drive.DriveError as exc:
            account_report["status"] = "error"
            report["errors"].append(f"{email}: {exc}")
            report["accounts"].append(account_report)
            continue

        for archive in archives:
            file_id = archive.get("id")
            modified = archive.get("modifiedTime") or ""
            name = archive.get("name") or file_id
            if storage.is_archive_processed(conn, file_id, modified):
                report["archives_skipped"] += 1
                continue
            try:
                added = _ingest_one_archive(
                    conn, creds, archive, email, log_path=log_path, known=known
                )
            except Exception as exc:
                report["errors"].append(f"{email}: archive {name!r}: {exc}")
                continue
            account_report["archives"] += 1
            account_report["visits_logged"] += added
            report["archives_processed"] += 1
            report["visits_logged"] += added

        report["accounts"].append(account_report)
    return report


def _ingest_one_archive(conn, creds, archive, email, log_path=None, known=None) -> int:
    """Download one archive, keep only its Wikipedia visits, discard the rest.

    The archive lands in a private temp directory that is removed in a
    finally block. Only the filtered Wikipedia subset is ever written into
    the project's data directory: the full export is never persisted here.
    """
    file_id = archive["id"]
    name = archive.get("name") or file_id
    modified = archive.get("modifiedTime") or ""

    workdir = Path(tempfile.mkdtemp(prefix="wikiwrapped-takeout-"))
    try:
        local_zip = drive.download_archive(creds, file_id, workdir / "archive.zip")
        try:
            rows = extract_history.load_takeout_zip(local_zip)
        except extract_history.NoHistoryInArchive:
            # One part of a multi-part export, carrying Calendar/Bookmarks/etc.
            # Record it so it is not downloaded again, and say nothing louder
            # than INFO: this is normal, not a failure.
            log.info(
                "Takeout %s (%s): no Chrome history in this part; skipping.",
                name, email,
            )
            storage.record_processed_archive(
                conn, file_id=file_id, modified_time=modified, account=email,
                file_name=name, visits_extracted=0,
            )
            conn.commit()
            return 0
        # Filter to Wikipedia BEFORE anything is retained.
        visits, counts = extract_history.build_visits(rows)
        records = [
            visitlog.record(
                visit["url"],
                visit["title"],
                visit["timestamp"],
                visitlog.SOURCE_TAKEOUT,
                account=email,
            )
            for visit in visits
        ]
        added = visitlog.append(records, path=log_path, known=known)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    storage.record_processed_archive(
        conn,
        file_id=file_id,
        modified_time=modified,
        account=email,
        file_name=name,
        visits_extracted=counts["kept"],
    )
    conn.commit()
    if counts["bad_timestamp"]:
        # Never let this be quiet. An epoch mismatch drops every row as
        # implausible, which otherwise looks exactly like "you have no
        # Wikipedia history" -- the failure that hid 2804 visits until a
        # real archive was finally inspected (see usec_to_unix).
        log.warning(
            "Takeout %s (%s): %d Wikipedia row(s) had unusable timestamps and "
            "were dropped. If that is most of them, the export's timestamp "
            "format has changed; check extract_history.usec_to_unix.",
            name, email, counts["bad_timestamp"],
        )
    log.info(
        "Takeout %s (%s): %d Wikipedia visits kept of %d rows "
        "(%d non-Wikipedia, %d bad timestamp, %d duplicate), %d new to the log.",
        name, email, counts["kept"], counts["read"], counts["non_wikipedia"],
        counts["bad_timestamp"], counts["duplicate"], added,
    )
    return added


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def run(
    db_path: str | Path | None = None,
    settings: dict | None = None,
    log_path: str | Path | None = None,
    force: bool = False,
    on_launch: bool = False,
    use_drive: bool = True,
    use_local: bool = True,
    rebuild: bool = False,
) -> dict:
    """One sync pass. Returns a report; never raises for expected failures."""
    started = dt.datetime.now(dt.timezone.utc)
    settings = localconfig.load() if settings is None else settings
    state = load_state()
    report: dict = {
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skipped": False,
        "reason": None,
        "local": None,
        "takeout": None,
        "ingest": None,
        "errors": [],
        "reauthorize": [],
    }

    # Cheap check, early exit. Takeout produces a new archive every ~2 months,
    # so there is nothing to gain from checking on every relaunch.
    if on_launch and not localconfig.sync_on_launch(settings):
        report.update(skipped=True, reason="sync_on_launch is off in config.local.toml")
        return report
    if not force:
        elapsed = hours_since_last_sync(state)
        interval = localconfig.min_sync_interval_hours(settings)
        if elapsed is not None and elapsed < interval:
            report.update(
                skipped=True,
                reason=f"synced {elapsed:.1f}h ago (interval {interval:g}h); "
                       "use --force to override",
            )
            return report

    known = visitlog.existing_keys(log_path)
    before = len(known)

    if use_local:
        report["local"] = sync_local_chrome(
            log_path=log_path,
            known=known,
            only=localconfig.chrome_profiles(settings),
        )
        report["errors"].extend(report["local"]["errors"])

    conn = None
    try:
        conn = storage.connect(db_path)

        if use_drive and localconfig.takeout_enabled(settings):
            report["takeout"] = sync_takeout(
                conn, settings, log_path=log_path, known=known
            )
            report["errors"].extend(report["takeout"]["errors"])
            report["reauthorize"] = report["takeout"]["reauthorize"]
        elif use_drive:
            report["takeout"] = {"skipped": "Takeout integration is disabled"}

        newly_logged = len(known) - before
        report["visits_logged"] = newly_logged
        report["ingest"] = _update_database(
            conn, log_path=log_path, newly_logged=newly_logged, rebuild=rebuild
        )
        if report["ingest"].get("error"):
            report["errors"].append(report["ingest"]["error"])
    except Exception as exc:
        # Last resort: whatever happened, the dashboard still starts.
        report["errors"].append(f"Sync failed: {exc}")
        log.exception("Sync pass failed")
    finally:
        if conn is not None:
            conn.close()

    finished = dt.datetime.now(dt.timezone.utc)
    report["finished_at"] = finished.strftime("%Y-%m-%dT%H:%M:%SZ")
    report["log"] = visitlog.stats(log_path)

    state.update(
        {
            "last_attempt": report["started_at"],
            "last_finished": report["finished_at"],
            "ok": not report["errors"],
            "errors": report["errors"][:10],
            "reauthorize": report["reauthorize"],
            "visits_logged": report.get("visits_logged", 0),
        }
    )
    if not report["errors"]:
        state["last_success"] = report["finished_at"]
    save_state(state)
    return report


def _update_database(conn, log_path=None, newly_logged=0, rebuild=False) -> dict:
    """Ingest log records into the DB.

    Normally only what this pass appended is ingested. Three cases force a
    full re-ingest of the log:

    - --rebuild, or an empty/missing DB (the log is the source of truth, so
      a deleted app.db is rebuilt rather than being an error).
    - A non-zero dedup tolerance. Incremental ingest can only dedupe within
      one batch, so a Takeout record arriving a second off a chrome_local
      visit logged last week would not be matched. At tolerance 0 that
      cannot happen, because an exact-second match is exactly what the
      UNIQUE (page_id, timestamp) constraint already enforces.
    """
    try:
        empty_db = storage.count_visits(conn) == 0
    except Exception as exc:
        return {"error": f"Could not read the database: {exc}"}

    full = rebuild or empty_db or dedup.DEDUP_TOLERANCE_S > 0
    if not full and newly_logged == 0:
        return {"skipped": "no new visits", "visits_total": storage.count_visits(conn)}

    records = visitlog.read(log_path)
    if not full:
        # Re-ingesting the tail is cheap and idempotent; the cutoff is a
        # generous window rather than an exact slice so nothing is missed.
        records = records[-max(newly_logged * 2, 1000):]
    if not records:
        return {"skipped": "log is empty"}

    document = visitlog.to_document(records)
    try:
        result = ingest.ingest_document(document, conn)
    except Exception as exc:
        # Redirect resolution needs the network, which is not worth failing
        # startup over.
        return {
            "error": f"Ingest into the database failed ({exc}). Visits are "
                     "safe in the append-only log; retry with "
                     "'python -m app.sync --rebuild'.",
        }
    result["mode"] = "full" if full else "incremental"
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the visit log from local Chrome and Google "
        "Takeout, then update the database."
    )
    parser.add_argument("--db", type=Path, default=None, help="Database path")
    parser.add_argument("--force", action="store_true",
                        help="Sync even if the last one was recent")
    parser.add_argument("--on-launch", action="store_true",
                        help="Respect the sync_on_launch config gate (run.sh uses this)")
    parser.add_argument("--no-drive", action="store_true",
                        help="Skip Google Drive; read local Chrome only")
    parser.add_argument("--no-local", action="store_true",
                        help="Skip local Chrome; read Drive only")
    parser.add_argument("--rebuild", action="store_true",
                        help="Re-ingest the whole log into the database")
    parser.add_argument("--quiet", action="store_true",
                        help="Only print warnings and errors")
    parser.add_argument(
        "--import", dest="import_path", type=Path, default=None, metavar="FILE",
        help="Append an existing export (extractor JSON or Takeout .zip) to "
             "the log, then sync. Use this once to migrate history captured "
             "before the log existed.",
    )
    parser.add_argument(
        "--compact-log", dest="compact", action="store_true",
        help="Drop log records superseded by a more specific duplicate "
             "(e.g. the same visit logged before and after it gained an "
             "account). Rewrites the log; the database is unaffected.",
    )
    parser.add_argument(
        "--backfill-from-db", dest="backfill", action="store_true",
        help="Copy visits that exist only in the database into the log, so "
             "the log becomes the complete record. URLs are reconstructed "
             "from canonical titles.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # Syncing into the new location while the old log sits unmigrated in the
    # repo would quietly split the history in two. Say so; never auto-copy.
    legacy = migrate.legacy_status()
    if legacy:
        log.warning(legacy["message"])

    if args.import_path:
        try:
            imported = import_export(args.import_path)
        except Exception as exc:
            print(f"Import failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"Imported {imported['file']}: {imported['visits_in_file']} visit(s) "
            f"in the file, {imported['new_to_log']} new to the log."
        )

    if args.compact:
        result = visitlog.compact()
        print(
            f"Compacted the log: {result['before']} -> {result['after']} "
            f"record(s), {result['removed']} superseded duplicate(s) removed."
        )
        conn = storage.connect(args.db)
        try:
            dropped = storage.compact_visit_sources(conn)
            conn.commit()
        finally:
            conn.close()
        print(f"Compacted provenance: {dropped} superseded row(s) removed.")

    if args.backfill:
        conn = storage.connect(args.db)
        try:
            filled = backfill_from_db(conn)
        finally:
            conn.close()
        print(
            f"Backfilled {filled['new_to_log']} visit(s) into the log from the "
            f"database ({filled['db_only_visits']} had no log record). "
            "Their URLs were reconstructed from canonical titles."
        )

    report = run(
        db_path=args.db,
        force=args.force,
        on_launch=args.on_launch,
        use_drive=not args.no_drive,
        use_local=not args.no_local,
        rebuild=args.rebuild,
    )

    if report["skipped"]:
        if not args.quiet:
            print(f"Sync skipped: {report['reason']}")
        return 0

    print(_summarize(report))
    # Exit 0 even on errors: run.sh must go on to start the server. The
    # errors are in the report, in sync_state.json, and on /health.
    return 0


def _summarize(report: dict) -> str:
    lines = []
    local = report.get("local")
    if local:
        profiles = ", ".join(
            f"{p.get('display_name') or p['profile']} [{p['profile']}]: "
            f"{p['wikipedia_visits']}"
            for p in local["profiles"]
        )
        lines.append(
            f"Local Chrome: {local['visits_logged']} new visit(s) logged"
            + (f" from {profiles}" if profiles else "")
        )
    takeout = report.get("takeout")
    if takeout and "skipped" not in takeout:
        lines.append(
            f"Takeout: {takeout['archives_processed']} archive(s) ingested, "
            f"{takeout['archives_skipped']} already processed, "
            f"{takeout['visits_logged']} new visit(s) logged"
        )
    elif takeout:
        lines.append(f"Takeout: {takeout['skipped']}")

    ingested = report.get("ingest") or {}
    if "visits_inserted" in ingested:
        lines.append(
            f"Database: +{ingested['visits_inserted']} visit(s), "
            f"{ingested['visits_total']} total "
            f"({ingested.get('visits_deduplicated', 0)} row(s) merged as duplicates)"
        )
    elif ingested.get("skipped"):
        lines.append(f"Database: {ingested['skipped']}")

    for email in report.get("reauthorize", []):
        lines.append(f"ACTION NEEDED: re-authorize {email} "
                     f"(python scripts/setup_takeout.py --authorize {email})")
    for message in report.get("errors", []):
        lines.append(f"WARNING: {message}")
    return "\n".join(lines) or "Nothing to do."


if __name__ == "__main__":
    sys.exit(main())
