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


def test_a_connection_can_be_closed_from_another_thread(tmp_path):
    """FastAPI runs a request's get_db setup and teardown on thread-pool
    threads that may differ; closing must not raise there. (The page loads
    /dashboard, /jobs and /settings at once, which made this a real 500.)"""
    import threading

    conn = storage.connect(tmp_path / "app.db")
    errors = []

    def close_elsewhere():
        try:
            conn.close()
        except Exception as exc:          # pragma: no cover - the bug
            errors.append(exc)

    worker = threading.Thread(target=close_elsewhere)
    worker.start()
    worker.join()
    assert errors == []


def test_concurrent_dashboard_requests_all_succeed(tmp_path):
    """The page's own load pattern: several requests in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app import api

    app = api.create_app(db_path=tmp_path / "app.db", reference_dir=tmp_path / "none")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        paths = ["/dashboard", "/health", "/jobs", "/stats/summary"] * 8
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda p: client.get(p).status_code, paths))
    assert set(codes) == {200}
