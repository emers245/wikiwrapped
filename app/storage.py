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


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the app database with the schema applied."""
    path = config.DB_PATH if db_path is None else db_path
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
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


def visit_timestamps(conn: sqlite3.Connection) -> list[str]:
    return [
        row["timestamp"]
        for row in conn.execute("SELECT timestamp FROM visits ORDER BY timestamp")
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


def page_topics(conn: sqlite3.Connection) -> list[dict]:
    """One row per unique page with its cached topic labels (NULL if the
    page has not been labeled yet)."""
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT pages.canonical_title, topic_cache.macro_topic,
                   topic_cache.raw_label
            FROM pages
            LEFT JOIN topic_cache
                ON topic_cache.canonical_title = pages.canonical_title
            """
        )
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
