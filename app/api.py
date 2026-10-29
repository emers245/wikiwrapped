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
- GET  /jobs                  background job progress (sync + enrichment)
- POST /jobs/sync             start a sync + enrichment job now
- GET  /settings              first-run flag + everything Settings shows
- PUT  /settings              save the Settings screen / first-run answers
- GET  /settings/chrome-profiles      detected profiles with visit counts
- POST /settings/folders/inspect      check a folder before adding it
- POST /settings/client-secret        import an OAuth client secret (Advanced)
- POST /settings/accounts/authorize   browser sign-in for one account
- DELETE /settings/accounts/{email}   forget an account and its token

Every request must be addressed to the loopback host, and state-changing
requests from another site's page are refused; see local_only below.

Run:  python -m app.launcher   (the app window; see app/launcher.py)
 or:  uvicorn app.api:app --reload   (API only, for development)

create_app() exists so tests (and future deployments) can point the app
at a different database/reference directory, and so the launcher can hand
in the JobRunner it owns; the module-level `app` uses the config defaults.
Building an app has no side effects: a JobRunner starts nothing until a
job is requested.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import config
from app import (embeddings, ingest, jobs, localconfig, migrate, network,
                 pageviews, setup_service, stats, storage, sync, topics,
                 umap_space)

STATIC_DIR = Path(__file__).parent / "static"

# The only names this server answers to. It listens on 127.0.0.1 only, but
# that alone does not stop a web page from reaching it: see local_only.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _hostname(host_header: str) -> str:
    """'127.0.0.1:48213' -> '127.0.0.1' (the Host header carries the port)."""
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def create_app(
    db_path: str | Path | None = None,
    reference_dir: str | Path | None = None,
    job_runner: jobs.JobRunner | None = None,
    allowed_hosts: tuple[str, ...] = LOOPBACK_HOSTS,
) -> FastAPI:
    app = FastAPI(title="WikiWrapped", version=config.APP_VERSION)
    db_path = db_path or config.DB_PATH
    reference_dir = reference_dir or config.REFERENCE_DIR
    runner = job_runner or jobs.JobRunner()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Refuse requests that a web page could make on someone's behalf.

        Any page open in the user's browser can send requests to
        127.0.0.1, and some of these endpoints read browsing history or
        accept file paths. Two checks close that off:

        - Host must be a loopback name. A malicious site can point its own
          DNS name at 127.0.0.1 ("DNS rebinding") to read responses as if
          same-origin; its requests then still carry ITS name in Host.
        - A state-changing request whose Origin is not this server's own
          is refused. Browsers attach Origin to cross-site POSTs, including
          the plain form posts that need no CORS preflight. Requests with
          no Origin at all (curl, the app's own tools) are allowed.
        """
        host = request.headers.get("host", "")
        if _hostname(host) not in allowed_hosts:
            return PlainTextResponse("Forbidden: unexpected Host header.", 403)
        origin = request.headers.get("origin")
        if (request.method not in SAFE_METHODS and origin is not None
                and urlsplit(origin).netloc != host):
            return PlainTextResponse("Forbidden: cross-site request.", 403)
        return await call_next(request)

    def get_db():
        conn = storage.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def get_shown_db():
        """A connection that sees only the accounts the dashboard shows.

        For the read-only views (dashboard, stats, map, network, page
        detail). Health, ingest, enrichment and settings use get_db and so
        always see everything. See storage.show_only_accounts.
        """
        conn = storage.connect(db_path)
        try:
            storage.show_only_accounts(
                conn, localconfig.hidden_accounts(localconfig.load())
            )
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
        backdrop_limit: int = Query(10_000, ge=0), conn=Depends(get_shown_db)
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
            # What is being left out, so the page can say so (and compare
            # /health's unfiltered totals against the right number).
            "filter": {
                "hidden_accounts": localconfig.hidden_accounts(localconfig.load()),
                "visits_all": storage.count_all_visits(conn),
            },
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
            # Non-zero means an ingest has outrun enrichment; the launcher's
            # background job closes the gap and the dashboard header says so.
            "enrichment": storage.count_enrichment_gaps(conn),
            "sync": {
                "last_attempt": state.get("last_attempt"),
                "last_success": state.get("last_success"),
                "ok": state.get("ok"),
                "errors": state.get("errors", []),
                "reauthorize": state.get("reauthorize", []),
                # Watched folders, per folder: status is ok | missing |
                # unreadable | error, with zips found and per-file errors.
                "folders": state.get("folders", []),
            },
            # Non-null while visit history from before the move to the
            # per-user data dir is still sitting, uncopied, in the repo.
            "legacy_data": migrate.legacy_status(),
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

    # -- background jobs ---------------------------------------------------

    @app.get("/jobs")
    def job_status():
        """What the background job is doing; the window polls this."""
        return runner.snapshot()

    @app.post("/jobs/sync")
    def start_sync(enrich: bool = True):
        """Sync every source now (ignoring the interval), then enrich.

        409 if a job is already running rather than queueing a second one:
        two syncs at once would only race each other to the same result.
        """
        if not runner.start("sync", jobs.sync_steps(force=True, enrich=enrich)):
            raise HTTPException(409, "A sync is already running.")
        return runner.snapshot()

    # -- setup and Settings (all rules live in app/setup_service.py) --------

    def bad_request(exc: setup_service.SetupError):
        return HTTPException(400, str(exc))

    def settings_view(settings: dict, conn) -> dict:
        """setup_service.status plus the accounts found in the database,
        for the "Show in dashboard" box."""
        view = setup_service.status(settings)
        view["dashboard"] = {
            "accounts": setup_service.dashboard_accounts(conn, settings),
        }
        return view

    @app.get("/settings")
    def get_settings(conn=Depends(get_db)):
        return settings_view(localconfig.load(), conn)

    @app.get("/settings/chrome-profiles")
    def get_chrome_profiles():
        """Separate from /settings: counting visits reads every History DB."""
        return setup_service.chrome_profiles(localconfig.load())

    @app.put("/settings")
    def put_settings(changes: dict = Body(...), conn=Depends(get_db)):
        """Save the Settings screen (or the first-run answers) in one go.

        Validated in full before anything is written, so a rejected change
        leaves the file exactly as it was. Writing the file is also what
        ends "first run".
        """
        settings = localconfig.load()
        try:
            setup_service.apply(settings, changes)
        except setup_service.SetupError as exc:
            raise bad_request(exc)
        localconfig.save(settings)
        return settings_view(settings, conn)

    @app.post("/settings/folders/inspect")
    def inspect_folder(body: dict = Body(...)):
        return setup_service.inspect_folder(body.get("path"))

    @app.post("/settings/client-secret")
    def import_client_secret(body: dict = Body(...)):
        settings = localconfig.load()
        try:
            setup_service.import_client_secret(settings, body.get("path"))
        except setup_service.SetupError as exc:
            raise bad_request(exc)
        localconfig.save(settings)
        return setup_service.status(settings, inspect_folders=False)["takeout"]

    @app.post("/settings/accounts/authorize")
    def authorize_account(body: dict = Body(default={})):
        """Sign an account in through the system browser.

        Blocks (in FastAPI's thread pool, not the event loop) until the
        browser comes back or AUTHORIZE_TIMEOUT_S passes. The result is
        applied to settings loaded AFTER the wait, so anything saved while
        the user was signing in is not overwritten.
        """
        try:
            email = setup_service.authorize(
                localconfig.load(), email_hint=(body or {}).get("email_hint")
            )
        except setup_service.SetupError as exc:
            raise bad_request(exc)
        settings = setup_service.record_authorized(localconfig.load(), email)
        localconfig.save(settings)
        return {"email": email,
                "takeout": setup_service.status(settings, inspect_folders=False)["takeout"]}

    @app.delete("/settings/accounts/{email}")
    def remove_account(email: str):
        settings = setup_service.remove_account(localconfig.load(), email)
        localconfig.save(settings)
        return setup_service.status(settings, inspect_folders=False)["takeout"]

    # -- stats (S7.3) ------------------------------------------------------

    @app.get("/stats/summary")
    def stats_summary(conn=Depends(get_shown_db)):
        return stats.summary(conn)

    @app.get("/stats/top")
    def stats_top(n: int = Query(10, ge=1, le=500), conn=Depends(get_shown_db)):
        return stats.most_visited(conn, n)

    @app.get("/stats/heatmap")
    def stats_heatmap(tz: str | None = None, conn=Depends(get_shown_db)):
        return stats.weekday_hour_grid(conn, validate_tz(tz))

    @app.get("/stats/daily")
    def stats_daily(tz: str | None = None, conn=Depends(get_shown_db)):
        return stats.daily_series(conn, validate_tz(tz))

    @app.get("/stats/busiest-week")
    def stats_busiest_week(tz: str | None = None, conn=Depends(get_shown_db)):
        return stats.busiest_week(conn, validate_tz(tz))

    @app.get("/stats/topics")
    def stats_topics(conn=Depends(get_shown_db)):
        return stats.topic_distribution(conn)

    # -- map + page detail -------------------------------------------------

    @app.get("/map")
    def map_points(conn=Depends(get_shown_db)):
        return storage.get_map_points(conn)

    @app.get("/network")
    def network_graph(
        gap: int = Query(network.DEFAULT_GAP_S, ge=1), conn=Depends(get_shown_db)
    ):
        return network.graph_metrics(conn, gap)

    @app.get("/network/view", response_class=HTMLResponse)
    def network_view():
        page = Path(__file__).parent / "static" / "network.html"
        return page.read_text(encoding="utf-8")

    @app.get("/pages/{title:path}")
    def page_detail(title: str, conn=Depends(get_shown_db)):
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
