"""Showing only some accounts in the dashboard (storage.show_only_accounts).

The fixture history, by design, covers every case the filter has to get
right:

    Cat 2025-01-01  chrome_local  work@example.com
    Cat 2025-01-02  takeout       me@example.com
    Dog 2025-01-03  chrome_local  work@example.com  AND  takeout me@example.com
                                  (one physical visit seen by both)
    Owl 2025-01-04  chrome_local  ''   (a profile not signed in)
    Fox 2025-01-05  (no provenance row at all: pre-log history)
"""

import pytest
from fastapi.testclient import TestClient

import config
from app import api, ingest, localconfig, storage

LOCAL = "http://127.0.0.1"

ROWS = [
    ("Cat", "2025-01-01T10:00:00Z", "chrome_local", "work@example.com"),
    ("Cat", "2025-01-02T10:00:00Z", "takeout", "me@example.com"),
    ("Dog", "2025-01-03T10:00:00Z", "chrome_local", "work@example.com"),
    ("Dog", "2025-01-03T10:00:00Z", "takeout", "me@example.com"),
    ("Owl", "2025-01-04T10:00:00Z", "chrome_local", None),
]


def build_db(path):
    conn = storage.connect(path)
    storage.update_redirect_cache(conn, {t: t for t in ("Cat", "Dog", "Owl", "Fox")})
    ingest.ingest_document({
        "schema_version": "1.0", "source_browser": "chrome",
        "exported_at": "2026-01-01T00:00:00Z",
        "visits": [
            {"url": f"https://en.wikipedia.org/wiki/{title}", "title": title,
             "timestamp": stamp, "source": source, "account": account}
            for title, stamp, source, account in ROWS
        ] + [
            # No source at all: a plain export, recorded with no provenance.
            {"url": "https://en.wikipedia.org/wiki/Fox", "title": "Fox",
             "timestamp": "2025-01-05T10:00:00Z"},
        ],
    }, conn)
    storage.upsert_pageviews(conn, "Cat", "20250101", "20250105", 999)
    conn.commit()
    return conn


@pytest.fixture
def conn(tmp_path):
    connection = build_db(tmp_path / "app.db")
    yield connection
    connection.close()


def shown(conn):
    return sorted(
        (row["canonical_title"], row["timestamp"][:10])
        for row in conn.execute(
            "SELECT pages.canonical_title, visits.timestamp FROM visits "
            "JOIN pages ON pages.id = visits.page_id"
        )
    )


# ---------------------------------------------------------------------------
# The view itself
# ---------------------------------------------------------------------------

def test_account_summary_lists_every_account_with_sources(conn):
    summary = {e["account"]: e for e in storage.account_summary(conn)}
    assert summary["work@example.com"]["visits"] == 2
    assert summary["me@example.com"]["by_source"] == {"takeout": 2}
    # '' gathers both a not-signed-in profile and history with no record.
    assert summary[""]["visits"] == 2
    assert summary[""]["by_source"] == {"chrome_local": 1, "no_record": 1}
    assert list(summary)[-1] == ""          # unattributed listed last


def test_nothing_hidden_means_no_views_at_all(conn):
    storage.show_only_accounts(conn, [])
    temp = conn.execute("SELECT name FROM sqlite_temp_master").fetchall()
    assert temp == []
    assert len(shown(conn)) == 5


def test_hiding_an_account_keeps_visits_another_account_saw(conn):
    storage.show_only_accounts(conn, ["work@example.com"])
    assert shown(conn) == [
        ("Cat", "2025-01-02"),          # me
        ("Dog", "2025-01-03"),          # work AND me: still shown
        ("Fox", "2025-01-05"),          # no record -> unattributed
        ("Owl", "2025-01-04"),          # '' account
    ]


def test_hiding_both_accounts_leaves_only_unattributed(conn):
    storage.show_only_accounts(conn, ["work@example.com", "me@example.com"])
    assert [t for t, _ in shown(conn)] == ["Fox", "Owl"]


def test_hiding_unattributed_covers_empty_and_missing_provenance(conn):
    storage.show_only_accounts(conn, [""])
    assert [t for t, _ in shown(conn)] == ["Cat", "Cat", "Dog"]


def test_pages_follow_the_shown_visits(conn):
    storage.show_only_accounts(conn, ["me@example.com"])
    pages = {p["canonical_title"]: p for p in storage.all_pages(conn)}
    assert pages["Cat"]["visit_count"] == 1
    assert pages["Cat"]["first_seen"] == "2025-01-01T10:00:00Z"
    storage.show_only_accounts(conn, ["work@example.com", "me@example.com", ""])
    assert storage.all_pages(conn) == [] and storage.count_pages(conn) == 0


def test_the_real_tables_are_untouched_and_unwritable_through_the_view(conn):
    storage.show_only_accounts(conn, ["work@example.com", "me@example.com"])
    assert storage.count_visits(conn) == 2
    assert storage.count_all_visits(conn) == 5
    import sqlite3
    with pytest.raises(sqlite3.OperationalError, match="view"):
        storage.insert_visits(conn, 1, ["2030-01-01T00:00:00Z"])


def test_the_filter_is_per_connection(tmp_path):
    first = build_db(tmp_path / "app.db")
    other = storage.connect(tmp_path / "app.db")
    try:
        storage.show_only_accounts(first, ["work@example.com", "me@example.com"])
        assert storage.count_visits(first) == 2
        assert storage.count_visits(other) == 5
    finally:
        first.close()
        other.close()


def test_pageview_range_ignores_the_filter(conn):
    """The cache is keyed on the whole history's range; a narrower one
    would turn every popularity number into n/a."""
    from app import pageviews

    storage.show_only_accounts(conn, ["me@example.com"])
    assert pageviews.history_date_range(conn) == ("20250101", "20250105")


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", tmp_path / "config.local.toml")
    build_db(tmp_path / "app.db").close()
    app = api.create_app(db_path=tmp_path / "app.db", reference_dir=tmp_path / "none")
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client


def hide(*accounts):
    settings = localconfig.load()
    settings["dashboard"] = {"hidden_accounts": list(accounts)}
    localconfig.save(settings)


def test_every_dashboard_number_follows_the_filter(client):
    hide("work@example.com", "me@example.com")
    data = client.get("/dashboard").json()
    assert data["summary"]["total_visits"] == 2
    assert data["filter"] == {"hidden_accounts": ["work@example.com", "me@example.com"],
                              "visits_all": 5}
    assert sorted(p["title"] for p in data["pages"]) == ["Fox", "Owl"]
    assert client.get("/stats/summary").json()["total_visits"] == 2
    assert {n["title"] for n in client.get("/network").json()["nodes"]} == {"Fox", "Owl"}
    assert client.get("/pages/Cat").status_code == 404


def test_health_and_settings_always_see_everything(client):
    hide("work@example.com")
    assert client.get("/health").json()["visits"] == 5
    accounts = {a["account"]: a["shown"] for a in
                client.get("/settings").json()["dashboard"]["accounts"]}
    assert accounts == {"work@example.com": False, "me@example.com": True, "": True}


def test_pageviews_still_show_while_filtered(client):
    hide("work@example.com")
    cat = next(p for p in client.get("/dashboard").json()["pages"] if p["title"] == "Cat")
    assert cat["pageviews"] == 999


def test_hidden_accounts_are_saved_through_put_settings(client):
    response = client.put("/settings", json={"dashboard": {"hidden_accounts": [""]}})
    assert response.status_code == 200
    assert localconfig.hidden_accounts(localconfig.load()) == [""]
    shown_flags = {a["account"]: a["shown"] for a in response.json()["dashboard"]["accounts"]}
    assert shown_flags[""] is False
    assert client.get("/dashboard").json()["summary"]["total_visits"] == 3


def test_a_malformed_hidden_list_is_refused(client):
    response = client.put("/settings", json={"dashboard": {"hidden_accounts": "me"}})
    assert response.status_code == 400
