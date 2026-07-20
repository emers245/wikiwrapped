"""Tests for app.api (repo_spec S7.7) via FastAPI's TestClient against a
temp-file DB (each request opens its own connection, so :memory: would
give every request a fresh empty DB). /enrich's module calls are
monkeypatched; the modules themselves have their own suites."""

import pytest
from fastapi.testclient import TestClient

from app import api, ingest, storage
from fakes import FakeResponse, FakeSession, api_payload


@pytest.fixture
def client(tmp_path):
    test_app = api.create_app(
        db_path=tmp_path / "test.db", reference_dir=tmp_path / "no_reference"
    )
    with TestClient(test_app) as test_client:
        test_client.db_path = tmp_path / "test.db"
        yield test_client


DOC = {
    "schema_version": "1.0",
    "source_browser": "chrome",
    "exported_at": "2026-01-01T00:00:00Z",
    "visits": [
        {"url": "https://en.wikipedia.org/wiki/Jainism", "title": "t",
         "timestamp": "2025-01-01T10:00:00Z"},
        {"url": "https://en.wikipedia.org/wiki/Jainism", "title": "t",
         "timestamp": "2025-01-02T11:00:00Z"},
        {"url": "https://en.wikipedia.org/wiki/Cat", "title": "t",
         "timestamp": "2025-01-03T09:00:00Z"},
    ],
}


def seed(client):
    """Ingest DOC directly (offline redirect resolution) + enrich caches."""
    conn = storage.connect(client.db_path)
    ingest.ingest_document(
        DOC, conn, session=FakeSession([
            FakeResponse(api_payload(pages=["Jainism", "Cat"]))
        ])
    )
    storage.upsert_topic(conn, "Jainism", "Culture.Philosophy and religion",
                         "Culture.Philosophy and religion")
    storage.upsert_umap_coords(conn, {"Jainism": (1.5, -2.0)})
    storage.upsert_pageviews(conn, "Jainism", "20250101", "20250103", 12345)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------

def test_health_reports_counts_and_missing_reference(client):
    seed(client)
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["pages"] == 2 and data["visits"] == 3
    assert data["reference_space"] is None  # not built in this fixture


def test_ingest_document_body_and_idempotency(client, monkeypatch):
    from app import normalize

    monkeypatch.setattr(
        normalize, "resolve_redirects",
        lambda titles, session=None: {t: t for t in titles},
    )
    first = client.post("/ingest", json=DOC).json()
    assert first["visits_inserted"] == 3 and first["pages_new"] == 2
    again = client.post("/ingest", json=DOC).json()
    assert again["visits_inserted"] == 0


def test_ingest_rejects_bad_schema_and_empty(client):
    bad = client.post("/ingest", json={"schema_version": "9.9", "visits": []})
    assert bad.status_code == 400
    assert "schema_version" in bad.json()["detail"]
    assert client.post("/ingest").status_code == 400


def test_stats_endpoints(client):
    seed(client)
    assert client.get("/stats/summary").json() == {
        "unique_pages": 2, "total_visits": 3,
        "first_visit": "2025-01-01T10:00:00Z",
        "last_visit": "2025-01-03T09:00:00Z",
    }
    top = client.get("/stats/top", params={"n": 1}).json()
    assert [(p["canonical_title"], p["visit_count"]) for p in top] == [
        ("Jainism", 2)]

    heatmap = client.get("/stats/heatmap", params={"tz": "UTC"}).json()
    assert heatmap["timezone"] == "UTC"
    assert sum(sum(row) for row in heatmap["counts"]) == 3

    daily = client.get("/stats/daily", params={"tz": "UTC"}).json()
    assert [d["visits"] for d in daily["days"]] == [1, 1, 1]

    week = client.get("/stats/busiest-week", params={"tz": "UTC"}).json()
    assert week["visits"] == 3

    dist = client.get("/stats/topics").json()
    assert dist["total_pages"] == 2
    assert {t["macro_topic"] for t in dist["macro_topics"]} == {
        "Culture.Philosophy and religion", "Unclassified"}


def test_bad_timezone_is_400(client):
    assert client.get("/stats/heatmap",
                      params={"tz": "Mars/Olympus"}).status_code == 400


def test_map_payload(client):
    seed(client)
    assert client.get("/map").json() == [{
        "title": "Jainism", "x": 1.5, "y": -2.0,
        "macro_topic": "Culture.Philosophy and religion", "visit_count": 2,
    }]


def test_page_detail_and_underscore_form(client):
    seed(client)
    detail = client.get("/pages/Jainism").json()
    assert detail["visit_count"] == 2
    assert detail["visits"] == ["2025-01-01T10:00:00Z", "2025-01-02T11:00:00Z"]
    assert detail["topic"]["macro_topic"] == "Culture.Philosophy and religion"
    assert detail["coords"] == [1.5, -2.0]
    assert detail["pageviews"] == {
        "date_start": "20250101", "date_end": "20250103",
        "total_views": 12345, "no_data": False,
    }
    # underscore form resolves; Cat has no enrichment yet
    cat = client.get("/pages/Cat").json()
    assert cat["topic"] is None and cat["coords"] is None
    assert cat["pageviews"] is None
    assert client.get("/pages/Jainism".replace("Jainism", "Jainism")).status_code == 200
    assert client.get("/pages/No_Such_Page").status_code == 404


def test_enrich_composes_module_reports(client, monkeypatch):
    from app import embeddings, pageviews, topics, umap_space

    def missing_reference(conn, reference_dir=None):
        raise FileNotFoundError(
            "artifact missing; run scripts/build_reference_space.py"
        )

    monkeypatch.setattr(topics, "label_pages", lambda conn: {"pages_labeled": 1})
    monkeypatch.setattr(embeddings, "embed_pages",
                        lambda conn: {"pages_embedded": 1})
    monkeypatch.setattr(pageviews, "fetch_all", lambda conn: {"pages_fetched": 1})
    monkeypatch.setattr(umap_space, "project_pages", missing_reference)
    report = client.post("/enrich", params={"include_pageviews": True}).json()
    assert report["topics"] == {"pages_labeled": 1}
    assert report["embeddings"] == {"pages_embedded": 1}
    assert "build_reference_space" in report["projection"]["error"]
    assert report["pageviews"] == {"pages_fetched": 1}
