"""SQLite schema + thin data-access layer (repo_spec S6, D6).

All SQL in the application lives in this module; other modules call typed
helpers and never write queries. SQLite over DuckDB (decision 2026-07-19):
stdlib, zero added dependency risk, sufficient for this data scale; the
layer is kept thin so a swap stays cheap.

Tables (S6, plus redirect_cache supporting D5/D6):
- pages/visits: one row per unique article vs one row per visit (D2).
- redirect_cache: raw title -> canonical title; NULL canonical means the
  title does not resolve to an existing article, cached so it is never
  re-queried.
- topic_cache, pageview_cache, embedding_cache, umap_coords: created here;
  their access helpers arrive with their modules (Steps 6-9).

Connections are passed explicitly; callers own transaction commit.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import numpy as np

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    canonical_title TEXT NOT NULL UNIQUE,
    raw_title TEXT,
    first_seen TEXT,
    visit_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id),
    timestamp TEXT NOT NULL,
    UNIQUE (page_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_visits_timestamp ON visits (timestamp);

-- Which mechanisms and accounts saw each visit (D10). A join table rather
-- than columns on visits: one physical visit can arrive from both the local
-- Chrome read and a Takeout export, and UNIQUE (page_id, timestamp) keeps
-- only one visits row, so a single source column would silently record
-- whichever landed first. Here both rows survive without inflating counts.
--
-- account is NOT NULL DEFAULT '' rather than nullable because SQLite treats
-- NULLs as distinct in a unique index, which would let the same
-- (visit, source) pair insert over and over. '' means "unknown account":
-- the local Chrome reader cannot tell which account a profile is signed
-- into without parsing Chrome's preferences.
CREATE TABLE IF NOT EXISTS visit_sources (
    visit_id INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (visit_id, source, account)
);
CREATE INDEX IF NOT EXISTS idx_visit_sources_source
    ON visit_sources (source, account);

-- Takeout archives already ingested, so repeated syncs are idempotent.
-- Files are never deleted from Drive, so every sync re-sees every archive
-- and this table is what stops it re-downloading them.
--
-- Keyed on (file_id, modified_time), not on file_id alone: a re-uploaded
-- archive keeps its ID but gets a new modified time and does need
-- reprocessing. account is informational only, because Drive file IDs are
-- globally unique; keying on it would reprocess an archive shared with a
-- second authorized account.
CREATE TABLE IF NOT EXISTS processed_archives (
    file_id TEXT NOT NULL,
    modified_time TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    file_name TEXT,
    ingested_at TEXT NOT NULL,
    visits_extracted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_id, modified_time)
);

CREATE TABLE IF NOT EXISTS redirect_cache (
    raw_title TEXT PRIMARY KEY,
    canonical_title TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_cache (
    canonical_title TEXT PRIMARY KEY,
    macro_topic TEXT,
    raw_label TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pageview_cache (
    canonical_title TEXT NOT NULL,
    date_start TEXT NOT NULL,
    date_end TEXT NOT NULL,
    total_views INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (canonical_title, date_start, date_end)
);

CREATE TABLE IF NOT EXISTS embedding_cache (
    canonical_title TEXT NOT NULL,
    model_name TEXT NOT NULL,
    vector BLOB NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (canonical_title, model_name)
);

CREATE TABLE IF NOT EXISTS umap_coords (
    canonical_title TEXT PRIMARY KEY,
    x REAL NOT NULL,
    y REAL NOT NULL
);
"""


# How long a connection waits for another one's write lock before giving up
# with "database is locked". Generous, because the longest writer is a sync
# ingesting a first Takeout archive, and waiting beats failing a request.
BUSY_TIMEOUT_S = 30


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the app database with the schema applied.

    WAL (write-ahead log) journal mode, because the launcher runs the sync
    and enrichment as child processes WHILE the API serves the dashboard
    from the same file. In SQLite's default rollback mode a writer
    committing locks readers out, and a long reader blocks the writer; in
    WAL mode readers never block the writer and the writer never blocks
    readers. There is still only one writer at a time, which the job
    runner's one-job rule and BUSY_TIMEOUT_S cover. The setting is stored
    in the database file, so this is a no-op after the first connection.
    WAL needs the -wal/-shm files beside the database on a local disk,
    which the per-user data directory is.
    """
    path = config.DB_PATH if db_path is None else db_path
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI opens a request's connection in one
    # thread-pool thread and may run the dependency's teardown (close) in
    # another, which sqlite3's default thread check turns into a 500 as soon
    # as the page makes several requests at once. Safe here because every
    # connection belongs to exactly one request or one script and is never
    # used from two threads at the same time.
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# pages / visits (D2)
# ---------------------------------------------------------------------------

def upsert_page(
    conn: sqlite3.Connection,
    canonical_title: str,
    raw_title: str | None,
    first_seen: str,
) -> int:
    """Insert or update one page; first_seen only ever moves earlier.

    ISO-8601 UTC strings compare lexicographically in time order, so MIN()
    is chronological.
    """
    conn.execute(
        """
        INSERT INTO pages (canonical_title, raw_title, first_seen)
        VALUES (?, ?, ?)
        ON CONFLICT (canonical_title) DO UPDATE SET
            first_seen = MIN(pages.first_seen, excluded.first_seen)
        """,
        (canonical_title, raw_title, first_seen),
    )
    row = conn.execute(
        "SELECT id FROM pages WHERE canonical_title = ?", (canonical_title,)
    ).fetchone()
    return row["id"]


def insert_visits(
    conn: sqlite3.Connection, page_id: int, timestamps: list[str]
) -> int:
    """Insert visits, silently skipping (page_id, timestamp) duplicates so
    re-ingesting an export is idempotent. Returns the number inserted."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO visits (page_id, timestamp) VALUES (?, ?)",
        [(page_id, stamp) for stamp in timestamps],
    )
    return conn.total_changes - before


def attach_visit_sources(
    conn: sqlite3.Connection,
    page_id: int,
    provenance: dict[str, list[dict]],
) -> int:
    """Record which sources/accounts saw each of a page's visits (D10).

    provenance maps an ISO timestamp to [{"source": ..., "account": ...}].
    Timestamps with no matching visits row are skipped: that means dedup
    collapsed them onto a neighbouring second, and the surviving row already
    carries the provenance.

    Returns the number of (visit, source, account) rows newly inserted.
    """
    if not provenance:
        return 0

    stamps = list(provenance)
    ids: dict[str, int] = {}
    for start in range(0, len(stamps), 500):  # stay under SQLite's param cap
        chunk = stamps[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT id, timestamp FROM visits "
            f"WHERE page_id = ? AND timestamp IN ({placeholders})",
            [page_id, *chunk],
        ):
            ids[row["timestamp"]] = row["id"]

    rows = [
        (ids[stamp], entry["source"], entry.get("account") or "")
        for stamp, entries in provenance.items()
        if stamp in ids
        for entry in entries
        if entry.get("source")
    ]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO visit_sources (visit_id, source, account) "
        "VALUES (?, ?, ?)",
        rows,
    )
    return conn.total_changes - before


def compact_visit_sources(conn: sqlite3.Connection) -> int:
    """Drop account-less provenance rows superseded by an accounted one.

    The mirror of visitlog.compact(). A visit first recorded without an
    account (an import that carried no profile information) and later
    re-recorded with one ends up with both rows, and the empty one is the
    less specific version of the same observation, so it double-counts in
    visit_source_summary.

    Only removes a row when the SAME visit and SAME source also has a real
    account. Two different accounts on one visit are both genuine and kept.
    Returns the number of rows removed.
    """
    before = conn.total_changes
    conn.execute(
        """
        DELETE FROM visit_sources
        WHERE account = ''
          AND EXISTS (
              SELECT 1 FROM visit_sources AS other
              WHERE other.visit_id = visit_sources.visit_id
                AND other.source = visit_sources.source
                AND other.account <> ''
          )
        """
    )
    return conn.total_changes - before


def visits_without_provenance(conn: sqlite3.Connection) -> list[dict]:
    """Visits with no visit_sources row: present in the DB but not the log.

    These predate the append-only log, so the DB is currently their only
    copy and deleting app.db would lose them. Returned as
    (canonical_title, timestamp) so the log can be backfilled.
    """
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT pages.canonical_title, pages.raw_title, visits.timestamp
            FROM visits JOIN pages ON pages.id = visits.page_id
            WHERE visits.id NOT IN (SELECT visit_id FROM visit_sources)
            ORDER BY visits.timestamp
            """
        )
    ]


def visit_source_summary(conn: sqlite3.Connection) -> list[dict]:
    """Per (source, account): how many visits it accounts for.

    Rows sum to more than the visit count when two mechanisms saw the same
    visit, which is the point of the table.
    """
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT source, account, COUNT(*) AS visits
            FROM visit_sources
            GROUP BY source, account
            ORDER BY source, account
            """
        )
    ]


def visit_sources_for(conn: sqlite3.Connection, visit_id: int) -> list[dict]:
    return [
        {"source": row["source"], "account": row["account"] or None}
        for row in conn.execute(
            "SELECT source, account FROM visit_sources WHERE visit_id = ? "
            "ORDER BY source, account",
            (visit_id,),
        )
    ]


def count_multi_source_visits(conn: sqlite3.Connection) -> int:
    """Visits seen by more than one source: the dedup work actually done."""
    return conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT visit_id FROM visit_sources
            GROUP BY visit_id HAVING COUNT(DISTINCT source) > 1
        )
        """
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# processed_archives (D10): what makes repeated Takeout syncs idempotent
# ---------------------------------------------------------------------------

def is_archive_processed(
    conn: sqlite3.Connection, file_id: str, modified_time: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_archives WHERE file_id = ? AND modified_time = ?",
        (file_id, modified_time),
    ).fetchone()
    return row is not None


def record_processed_archive(
    conn: sqlite3.Connection,
    file_id: str,
    modified_time: str,
    account: str | None,
    file_name: str | None,
    visits_extracted: int,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO processed_archives "
        "(file_id, modified_time, account, file_name, ingested_at, "
        " visits_extracted) VALUES (?, ?, ?, ?, ?, ?)",
        (
            file_id,
            modified_time,
            account or "",
            file_name,
            utcnow_iso(),
            visits_extracted,
        ),
    )


def processed_archives(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM processed_archives ORDER BY ingested_at DESC"
        )
    ]


def refresh_visit_counts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE pages SET visit_count = (
            SELECT COUNT(*) FROM visits WHERE visits.page_id = pages.id
        )
        """
    )


def count_pages(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]


def count_visits(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]


def count_enrichment_gaps(conn: sqlite3.Connection) -> dict[str, int]:
    """How many pages /enrich has not caught up with yet.

    Ingestion and enrichment are separate passes, so a bulk import leaves
    pages with no topic row and no projected coordinates. Both gaps are
    silent in the UI - one shows up as "Unclassified", the other simply as
    a page missing from the map - so they are counted explicitly here.
    """
    return {
        name: conn.execute(
            f"""
            SELECT COUNT(*) FROM pages
            LEFT JOIN {table} ON {table}.canonical_title = pages.canonical_title
            WHERE {table}.canonical_title IS NULL
            """
        ).fetchone()[0]
        for name, table in (
            ("topics_missing", "topic_cache"),
            ("coords_missing", "umap_coords"),
        )
    }


def visit_timestamps(conn: sqlite3.Connection) -> list[str]:
    return [
        row["timestamp"]
        for row in conn.execute("SELECT timestamp FROM visits ORDER BY timestamp")
    ]


def visit_sequence(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(canonical_title, timestamp) for every visit in time order; the
    input for session-transition edges."""
    return [
        (row["canonical_title"], row["timestamp"])
        for row in conn.execute(
            """
            SELECT pages.canonical_title, visits.timestamp
            FROM visits JOIN pages ON pages.id = visits.page_id
            ORDER BY visits.timestamp
            """
        )
    ]


def visit_date_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM visits").fetchone()
    return row[0], row[1]


def top_pages(conn: sqlite3.Connection, limit: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT canonical_title, visit_count, first_seen
            FROM pages
            ORDER BY visit_count DESC, canonical_title ASC
            LIMIT ?
            """,
            (limit,),
        )
    ]


def get_page(conn: sqlite3.Connection, canonical_title: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pages WHERE canonical_title = ?", (canonical_title,)
    ).fetchone()
    return dict(row) if row else None


def page_visit_timestamps(conn: sqlite3.Connection, page_id: int) -> list[str]:
    return [
        row["timestamp"]
        for row in conn.execute(
            "SELECT timestamp FROM visits WHERE page_id = ? ORDER BY timestamp",
            (page_id,),
        )
    ]


def get_map_points(conn: sqlite3.Connection) -> list[dict]:
    """Projected pages with topic + visit count: the /map payload."""
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT pages.canonical_title AS title, umap_coords.x, umap_coords.y,
                   topic_cache.macro_topic, pages.visit_count
            FROM pages
            JOIN umap_coords
                ON umap_coords.canonical_title = pages.canonical_title
            LEFT JOIN topic_cache
                ON topic_cache.canonical_title = pages.canonical_title
            ORDER BY pages.canonical_title
            """
        )
    ]


def get_topic(conn: sqlite3.Connection, canonical_title: str) -> dict | None:
    row = conn.execute(
        "SELECT macro_topic, raw_label FROM topic_cache WHERE canonical_title = ?",
        (canonical_title,),
    ).fetchone()
    return dict(row) if row else None


def page_topics(conn: sqlite3.Connection) -> list[dict]:
    """One row per unique page with its cached topic labels (NULL if the
    page has not been labeled yet).

    has_topic_row separates the two ways the labels can be NULL: 1 means
    the model was asked and returned nothing, 0 means it was never asked.
    Both render as "Unclassified", but only the second is a missing step.
    """
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT pages.canonical_title, topic_cache.macro_topic,
                   topic_cache.raw_label,
                   topic_cache.canonical_title IS NOT NULL AS has_topic_row
            FROM pages
            LEFT JOIN topic_cache
                ON topic_cache.canonical_title = pages.canonical_title
            """
        )
    ]


def all_pages(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute("SELECT * FROM pages ORDER BY canonical_title")
    ]


def all_page_titles(conn: sqlite3.Connection) -> list[str]:
    return [
        row["canonical_title"]
        for row in conn.execute(
            "SELECT canonical_title FROM pages ORDER BY canonical_title"
        )
    ]


# ---------------------------------------------------------------------------
# topic_cache (D3, D6)
# ---------------------------------------------------------------------------

def get_topic_cache(conn: sqlite3.Connection) -> dict[str, dict]:
    """Full cache load: title -> {macro_topic, raw_label}. NULL labels mean
    "known unclassifiable, do not re-fetch" (D6)."""
    return {
        row["canonical_title"]: {
            "macro_topic": row["macro_topic"],
            "raw_label": row["raw_label"],
        }
        for row in conn.execute(
            "SELECT canonical_title, macro_topic, raw_label FROM topic_cache"
        )
    }


def upsert_topic(
    conn: sqlite3.Connection,
    canonical_title: str,
    macro_topic: str | None,
    raw_label: str | None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO topic_cache "
        "(canonical_title, macro_topic, raw_label, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        (canonical_title, macro_topic, raw_label, utcnow_iso()),
    )


# ---------------------------------------------------------------------------
# embedding_cache (D6): vectors stored as float32 BLOBs
# ---------------------------------------------------------------------------

def embedded_titles(conn: sqlite3.Connection, model_name: str) -> set[str]:
    return {
        row["canonical_title"]
        for row in conn.execute(
            "SELECT canonical_title FROM embedding_cache WHERE model_name = ?",
            (model_name,),
        )
    }


def upsert_embedding(
    conn: sqlite3.Connection,
    canonical_title: str,
    model_name: str,
    vector: np.ndarray,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO embedding_cache "
        "(canonical_title, model_name, vector, fetched_at) VALUES (?, ?, ?, ?)",
        (
            canonical_title,
            model_name,
            np.asarray(vector, dtype=np.float32).tobytes(),
            utcnow_iso(),
        ),
    )


def get_embeddings(
    conn: sqlite3.Connection, model_name: str, titles: list[str]
) -> dict[str, np.ndarray]:
    """Cached vectors for the requested titles (missing ones absent from
    the result). Queries in chunks to stay under SQLite's parameter cap."""
    out: dict[str, np.ndarray] = {}
    unique = list(dict.fromkeys(titles))
    for start in range(0, len(unique), 500):
        chunk = unique[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT canonical_title, vector FROM embedding_cache "
            f"WHERE model_name = ? AND canonical_title IN ({placeholders})",
            [model_name, *chunk],
        ):
            out[row["canonical_title"]] = np.frombuffer(
                row["vector"], dtype=np.float32
            )
    return out


# ---------------------------------------------------------------------------
# pageview_cache (D6, D7): keyed by title + date range
# ---------------------------------------------------------------------------

def get_pageview_cache(
    conn: sqlite3.Connection, date_start: str, date_end: str
) -> dict[str, int | None]:
    """title -> total_views for one date range. NULL total_views means the
    API had no data for that title/range (cached so it is not re-asked)."""
    return {
        row["canonical_title"]: row["total_views"]
        for row in conn.execute(
            "SELECT canonical_title, total_views FROM pageview_cache "
            "WHERE date_start = ? AND date_end = ?",
            (date_start, date_end),
        )
    }


def upsert_pageviews(
    conn: sqlite3.Connection,
    canonical_title: str,
    date_start: str,
    date_end: str,
    total_views: int | None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pageview_cache "
        "(canonical_title, date_start, date_end, total_views, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (canonical_title, date_start, date_end, total_views, utcnow_iso()),
    )


# ---------------------------------------------------------------------------
# umap_coords (D4)
# ---------------------------------------------------------------------------

def pages_without_coords(conn: sqlite3.Connection) -> list[str]:
    return [
        row["canonical_title"]
        for row in conn.execute(
            """
            SELECT canonical_title FROM pages
            WHERE canonical_title NOT IN
                (SELECT canonical_title FROM umap_coords)
            ORDER BY canonical_title
            """
        )
    ]


def upsert_umap_coords(
    conn: sqlite3.Connection, coords: dict[str, tuple[float, float]]
) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO umap_coords (canonical_title, x, y) "
        "VALUES (?, ?, ?)",
        [(title, x, y) for title, (x, y) in coords.items()],
    )


def get_umap_coords(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    return {
        row["canonical_title"]: (row["x"], row["y"])
        for row in conn.execute("SELECT canonical_title, x, y FROM umap_coords")
    }


# ---------------------------------------------------------------------------
# redirect_cache (D5, D6)
# ---------------------------------------------------------------------------

def get_redirect_cache(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        row["raw_title"]: row["canonical_title"]
        for row in conn.execute(
            "SELECT raw_title, canonical_title FROM redirect_cache"
        )
    }


def update_redirect_cache(
    conn: sqlite3.Connection, mapping: dict[str, str | None]
) -> None:
    now = utcnow_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO redirect_cache "
        "(raw_title, canonical_title, fetched_at) VALUES (?, ?, ?)",
        [(raw, canonical, now) for raw, canonical in mapping.items()],
    )
