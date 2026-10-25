"""FastAPI app exposing computed results as JSON (repo_spec S7.7).

Thin by design: every endpoint delegates to a sibling module and returns
its data structure verbatim. This is the seam the Phase 2 frontend
consumes.

Endpoints:
- GET  /health                DB counts + reference-space metadata (or null)
- POST /ingest                standardized JSON as request body, or ?path=
- POST /enrich                topics + embeddings + projection
                              (+ pageviews with ?include_pageviews=true)
- GET  /stats/summary | /stats/top?n= | /stats/heatmap?tz= |
       /stats/daily?tz= | /stats/busiest-week?tz= | /stats/topics
- GET  /map                   projected pages: title, x, y, topic, visits
- GET  /pages/{title}         per-page detail (Phase 2 mouseover payload)

Run:  uvicorn app.api:app --reload

create_app() exists so tests (and future deployments) can point the app
at a different database/reference directory; the module-level `app` uses
the config defaults.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config
from app import (embeddings, ingest, network, pageviews, stats, storage, sync,
                 topics, umap_space)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    db_path: str | Path | None = None,
    reference_dir: str | Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Wikipedia Wrapped", version="0.1")
    db_path = db_path or config.DB_PATH
    reference_dir = reference_dir or config.REFERENCE_DIR

    def get_db():
        conn = storage.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def validate_tz(tz: str | None) -> str | None:
        try:
            config.get_timezone(tz)
        except Exception:
            raise HTTPException(400, f"Unknown IANA timezone: {tz!r}")
        return tz

    # -- frontend (Phase 2) ------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/dashboard")
    def dashboard(
        backdrop_limit: int = Query(10_000, ge=0), conn=Depends(get_db)
    ):
        """Everything the frontend needs in one load, so hover interactions
        never wait on the network."""
        graph = network.graph_metrics(conn)

        visits_by_title = defaultdict(list)
        for title, stamp in storage.visit_sequence(conn):
            visits_by_title[title].append(stamp)
        span = pageviews.history_date_range(conn)
        views = storage.get_pageview_cache(conn, *span) if span else {}
        first_seen = {
            page["canonical_title"]: page["first_seen"]
            for page in storage.all_pages(conn)
        }
        pages = [
            {
                **node,
                "first_seen": first_seen.get(node["title"]),
                "pageviews": views.get(node["title"]),
                "visits": visits_by_title.get(node["title"], []),
            }
            for node in graph["nodes"]
        ]

        try:
            points = umap_space.load_points(reference_dir)
            head = [p for p in points if p.get("source") == "head"]
            counts = Counter(
                (p.get("macro_topic") or "Unclassified") for p in head
            )
            total = sum(counts.values()) or 1
            backdrop = points
            if backdrop_limit and len(points) > backdrop_limit:
                step = len(points) / backdrop_limit
                backdrop = [points[int(i * step)] for i in range(backdrop_limit)]
            reference = {
                "points_total": len(points),
                "backdrop": [
                    {"x": p["x"], "y": p["y"], "macro_topic": p.get("macro_topic")}
                    for p in backdrop
                ],
                "head_fractions": [
                    {"macro_topic": topic, "fraction": round(count / total, 4)}
                    for topic, count in counts.most_common()
                ],
            }
        except FileNotFoundError:
            reference = None

        return {
            "summary": stats.summary(conn),
            "session_gap_s": graph["session_gap_s"],
            "pages": pages,
            "edges": graph["edges"],
            "reference": reference,
            "stats": {
                "topics": stats.topic_distribution(conn),
                "daily": stats.daily_series(conn),
                "hour_grid": stats.weekday_hour_grid(conn),
            },
        }

    # -- pipeline ----------------------------------------------------------

    @app.get("/health")
    def health(conn=Depends(get_db)):
        try:
            reference = umap_space.load_meta(reference_dir)
        except FileNotFoundError:
            reference = None
        # Sync state is read from the file the launcher's sync pass wrote,
        # not recomputed: /health must stay cheap and must never touch the
        # network. "reauthorize" naming an account is the signal the UI wants.
        state = sync.load_state()
        return {
            "status": "ok",
            "database": str(db_path),
            "pages": storage.count_pages(conn),
            "visits": storage.count_visits(conn),
            "reference_space": reference,
            "sources": storage.visit_source_summary(conn),
            # Non-zero means an ingest has outrun /enrich; run.sh checks
            # this on launch and the dashboard header surfaces it.
            "enrichment": storage.count_enrichment_gaps(conn),
            "sync": {
                "last_attempt": state.get("last_attempt"),
                "last_success": state.get("last_success"),
                "ok": state.get("ok"),
                "errors": state.get("errors", []),
                "reauthorize": state.get("reauthorize", []),
            },
        }

    @app.post("/ingest")
    def ingest_history(
        path: str | None = None,
        document: dict | None = Body(default=None),
        conn=Depends(get_db),
    ):
        try:
            if path is not None:
                return ingest.ingest_file(path, conn)
            if document is not None:
                return ingest.ingest_document(document, conn)
        except FileNotFoundError as exc:
            raise HTTPException(400, f"No such file: {exc.filename}")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        raise HTTPException(
            400, "Send the standardized history JSON as the request body, "
            "or pass ?path=/local/file.json"
        )

    @app.post("/enrich")
    def enrich(include_pageviews: bool = False, conn=Depends(get_db)):
        report = {
            "topics": topics.label_pages(conn),
            "embeddings": embeddings.embed_pages(conn),
        }
        try:
            report["projection"] = umap_space.project_pages(conn, reference_dir)
        except FileNotFoundError as exc:
            report["projection"] = {"error": str(exc)}
        if include_pageviews:
            report["pageviews"] = pageviews.fetch_all(conn)
        return report

    # -- stats (S7.3) ------------------------------------------------------

    @app.get("/stats/summary")
    def stats_summary(conn=Depends(get_db)):
        return stats.summary(conn)

    @app.get("/stats/top")
    def stats_top(n: int = Query(10, ge=1, le=500), conn=Depends(get_db)):
        return stats.most_visited(conn, n)

    @app.get("/stats/heatmap")
    def stats_heatmap(tz: str | None = None, conn=Depends(get_db)):
        return stats.weekday_hour_grid(conn, validate_tz(tz))

    @app.get("/stats/daily")
    def stats_daily(tz: str | None = None, conn=Depends(get_db)):
        return stats.daily_series(conn, validate_tz(tz))

    @app.get("/stats/busiest-week")
    def stats_busiest_week(tz: str | None = None, conn=Depends(get_db)):
        return stats.busiest_week(conn, validate_tz(tz))

    @app.get("/stats/topics")
    def stats_topics(conn=Depends(get_db)):
        return stats.topic_distribution(conn)

    # -- map + page detail -------------------------------------------------

    @app.get("/map")
    def map_points(conn=Depends(get_db)):
        return storage.get_map_points(conn)

    @app.get("/network")
    def network_graph(
        gap: int = Query(network.DEFAULT_GAP_S, ge=1), conn=Depends(get_db)
    ):
        return network.graph_metrics(conn, gap)

    @app.get("/network/view", response_class=HTMLResponse)
    def network_view():
        page = Path(__file__).parent / "static" / "network.html"
        return page.read_text(encoding="utf-8")

    @app.get("/pages/{title:path}")
    def page_detail(title: str, conn=Depends(get_db)):
        page = storage.get_page(conn, title)
        if page is None:  # accept underscore form too
            page = storage.get_page(conn, title.replace("_", " "))
        if page is None:
            raise HTTPException(404, f"No visited page titled {title!r}")

        detail = {
            **page,
            "visits": storage.page_visit_timestamps(conn, page["id"]),
            "topic": storage.get_topic(conn, page["canonical_title"]),
            "coords": storage.get_umap_coords(conn).get(page["canonical_title"]),
            "pageviews": None,
        }
        span = pageviews.history_date_range(conn)
        if span is not None:  # cached only; /enrich fetches (D7)
            cached = storage.get_pageview_cache(conn, *span)
            if page["canonical_title"] in cached:
                total = cached[page["canonical_title"]]
                detail["pageviews"] = {
                    "date_start": span[0],
                    "date_end": span[1],
                    "total_views": total or 0,
                    "no_data": total is None,
                }
        return detail

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
