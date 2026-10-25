"""The database is shared by the API (reading) and a sync child process
(writing) at the same time, which is what WAL mode in storage.connect is
for. These pin that down."""

import sqlite3

from app import storage


def test_databases_are_opened_in_wal_mode(tmp_path):
    conn = storage.connect(tmp_path / "app.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_a_reader_is_not_blocked_by_an_open_write_transaction(tmp_path):
    """The dashboard reading while a sync is mid-ingest must not see
    "database is locked", and must see only committed data."""
    path = tmp_path / "app.db"
    writer = storage.connect(path)
    storage.upsert_topic(writer, "Committed", None, None)
    writer.commit()

    writer.execute("BEGIN IMMEDIATE")
    storage.upsert_topic(writer, "In flight", None, None)
    try:
        reader = sqlite3.connect(str(path), timeout=0.5)
        try:
            titles = {row[0] for row in reader.execute(
                "SELECT canonical_title FROM topic_cache")}
        finally:
            reader.close()
        assert titles == {"Committed"}
    finally:
        writer.rollback()
        writer.close()


def test_in_memory_databases_still_work(tmp_path):
    conn = storage.connect(":memory:")
    try:
        assert storage.count_pages(conn) == 0
    finally:
        conn.close()
