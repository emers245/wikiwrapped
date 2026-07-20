"""Reading-session network metrics (repo_spec S11, promoted from Phase 2).

Edges are browsing transitions, ported from the old exploratory analysis:
consecutive visits to two different articles within a session gap (default
15 minutes) form a directed edge, weighted by how often that transition
occurred. Same-article repeats (reloads) are not self-loops.

Metrics per node: in_degree / out_degree (distinct articles linked from /
to) and PageRank (transition-weighted). Computed on the fly - at
personal-history scale this is milliseconds, so no cache table.

The /network endpoint returns nodes+edges shaped for a JS graph library
(cytoscape.js renders it at /network/view); UMAP coords ride along so the
frontend can use the semantic layout instead of a force layout.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import networkx as nx

from app import storage

DEFAULT_GAP_S = 900  # 15-minute session window, from the old analysis


def build_edges(
    conn: sqlite3.Connection, gap_s: int = DEFAULT_GAP_S
) -> dict[tuple[str, str], int]:
    """(source, target) -> transition count over the visit sequence."""
    edges: dict[tuple[str, str], int] = {}
    sequence = storage.visit_sequence(conn)
    for (prev_title, prev_stamp), (title, stamp) in zip(sequence, sequence[1:]):
        if title == prev_title:
            continue
        delta = (
            dt.datetime.fromisoformat(stamp)
            - dt.datetime.fromisoformat(prev_stamp)
        ).total_seconds()
        if delta <= gap_s:
            edges[(prev_title, title)] = edges.get((prev_title, title), 0) + 1
    return edges


def graph_metrics(conn: sqlite3.Connection, gap_s: int = DEFAULT_GAP_S) -> dict:
    """Nodes (with degrees + PageRank + topic/coords) and weighted edges."""
    edges = build_edges(conn, gap_s)
    graph = nx.DiGraph()
    for (source, target), weight in edges.items():
        graph.add_edge(source, target, weight=weight)
    pagerank = nx.pagerank(graph, weight="weight") if graph.number_of_nodes() else {}

    topics = storage.get_topic_cache(conn)
    coords = storage.get_umap_coords(conn)
    nodes = []
    for row in conn.execute(
        "SELECT canonical_title, visit_count FROM pages ORDER BY canonical_title"
    ):
        title = row["canonical_title"]
        point = coords.get(title)
        nodes.append({
            "title": title,
            "visit_count": row["visit_count"],
            "macro_topic": (topics.get(title) or {}).get("macro_topic"),
            "in_degree": graph.in_degree(title) if title in graph else 0,
            "out_degree": graph.out_degree(title) if title in graph else 0,
            "pagerank": round(pagerank.get(title, 0.0), 6),
            "x": point[0] if point else None,
            "y": point[1] if point else None,
        })
    return {
        "session_gap_s": gap_s,
        "nodes": nodes,
        "edges": [
            {"source": source, "target": target, "weight": weight}
            for (source, target), weight in sorted(edges.items())
        ],
    }
