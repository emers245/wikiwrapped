"""Tests for app.network (repo_spec S11): session-edge construction and
degree/PageRank metrics, plus the /network endpoints."""

import pytest
from fastapi.testclient import TestClient

from app import api, network, storage


@pytest.fixture
def conn():
    connection = storage.connect(":memory:")
    yield connection
    connection.close()


def add_visits(conn, title, timestamps):
    page_id = storage.upsert_page(conn, title, title, min(timestamps))
    storage.insert_visits(conn, page_id, timestamps)
    storage.refresh_visit_counts(conn)


def seed_chain(conn):
    # A -> B (twice, 1 min apart), B -> C once, C ... 20 min gap ... A
    add_visits(conn, "A", ["2025-01-01T10:00:00Z", "2025-01-01T11:00:00Z"])
    add_visits(conn, "B", ["2025-01-01T10:01:00Z", "2025-01-01T11:01:00Z"])
    add_visits(conn, "C", ["2025-01-01T11:02:00Z"])
    add_visits(conn, "Lonely", ["2025-01-02T09:00:00Z"])  # next day: no edge


def test_build_edges_gap_weights_and_no_self_loops(conn):
    add_visits(conn, "A", ["2025-01-01T10:00:00Z",
                           "2025-01-01T10:00:30Z"])  # reload: no self-loop
    add_visits(conn, "B", ["2025-01-01T10:01:00Z",
                           "2025-01-01T10:30:00Z"])  # 29 min later: too far
    assert network.build_edges(conn) == {("A", "B"): 1}
    # reloads never become self-loops even with a huge gap
    assert network.build_edges(conn, gap_s=3600) == {("A", "B"): 1}


def test_graph_metrics_degrees_and_pagerank(conn):
    seed_chain(conn)
    result = network.graph_metrics(conn)
    nodes = {n["title"]: n for n in result["nodes"]}

    assert result["edges"] == [
        {"source": "A", "target": "B", "weight": 2},
        {"source": "B", "target": "C", "weight": 1},
    ]
    assert (nodes["A"]["in_degree"], nodes["A"]["out_degree"]) == (0, 1)
    assert (nodes["B"]["in_degree"], nodes["B"]["out_degree"]) == (1, 1)
    assert (nodes["C"]["in_degree"], nodes["C"]["out_degree"]) == (1, 0)
    assert (nodes["Lonely"]["in_degree"], nodes["Lonely"]["out_degree"]) == (0, 0)
    # PageRank: sink C accumulates the most, isolated page gets 0
    assert nodes["C"]["pagerank"] > nodes["A"]["pagerank"] > 0
    assert nodes["Lonely"]["pagerank"] == 0.0
    ranks = sum(n["pagerank"] for n in result["nodes"])
    assert ranks == pytest.approx(1.0, abs=0.01)


def test_graph_metrics_empty_db(conn):
    result = network.graph_metrics(conn)
    assert result["nodes"] == [] and result["edges"] == []


def test_network_endpoints(tmp_path):
    app = api.create_app(db_path=tmp_path / "t.db",
                         reference_dir=tmp_path / "none")
    with TestClient(app) as client:
        conn = storage.connect(tmp_path / "t.db")
        seed_chain(conn)
        conn.commit()
        conn.close()

        data = client.get("/network").json()
        assert len(data["nodes"]) == 4 and len(data["edges"]) == 2
        assert data["session_gap_s"] == 900

        wide = client.get("/network", params={"gap": 100000}).json()
        assert len(wide["edges"]) >= 2  # the C -> Lonely overnight hop counts now

        view = client.get("/network/view")
        assert view.status_code == 200
        assert "cytoscape" in view.text
