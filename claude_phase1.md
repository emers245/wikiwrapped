# Wikipedia Wrapped - Phase 1 Implementation Plan

This plan implements Phase 1 of `repo_spec.md`: the full backend pipeline (extract -> ingest -> normalize -> store -> analyze -> serve) through a queryable FastAPI layer, with no frontend. It follows the spec's build order (S10) and honors all non-negotiable decisions D1-D8.

## Resolved decisions (confirmed with Joe, 2026-07-19)

| Question | Decision | Rationale |
|---|---|---|
| Database | **SQLite** | Stdlib - zero added dependency risk next to the fragile umap/numba pins; data volume (thousands of rows) doesn't need DuckDB's analytical engine. `storage.py` stays a thin data-access layer so a swap remains cheap. |
| Timezone | **Device timezone via `tzlocal`, overridable** in `config.py` and per-request on stats endpoints | Zero-config and DST-correct (IANA zone, not a fixed offset like the old UTC-6 hardcode). Override is the escape hatch for re-analysis or travel. |
| UMAP reference sample | **~40k articles (30k top-viewed "head" + 10k random "tail")** | Matches the old sampler's proven scale; good topic coverage; one-time ~1 hr CPU embedding cost is acceptable. |
| `pageviews.py` scope | **Client + cache built in Phase 1**; comparison analytics stay in Phase 2 | The API seam and cache table exist now, so Phase 2 popularity features bolt on without backend changes. |

## What the reference code contributes (and what gets fixed)

Logic to **port** (per D1's "fresh repo" rule - port logic, don't reuse files):

- **Timestamp handling** (`wikipediaWrapped.py` / `analyzeHistory.py`): Chrome `time_usec` (us since 1601, offset `11644473600 * 1e6`) and Takeout `visitTime`/`lastVisitTime` (ms since 1970) -> UTC ISO-8601. Goes into `extract/extract_history.py`.
- **Chrome History DB read** (`saveChromeHistory.py`): copy the locked SQLite file to a temp file first. Kept; the hardcoded macOS path gets replaced with per-OS defaults + `--input` override.
- **`macro()` collapsing** (`wikipediaSample.py`): first-level topic except `Culture.*` keeps second level. Ported unchanged into `topics.py`.
- **Head+tail sampling** (`wikipediaSample.py`): top-30k from a pageviews dump + 10k random via `list=random`. Ported into `scripts/build_reference_space.py`, with topic labels now coming from Lift Wing.
- **Summary fetch + embedding** (`wikipediaWrapped.py`, `wikipediaCluster.py`): REST summary endpoint (`description` + `extract` concatenated) -> `all-MiniLM-L6-v2`, normalized vectors, cosine metric. Ported into `embeddings.py`.
- **UMAP parameters** (`wikipediaWrapped.py`): `n_neighbors=30, min_dist=0.1, n_components=2, metric="cosine", random_state=42` as the starting configuration for the reference fit.
- **Stats logic**: weekdayxhour pivot, daily counts + 7-day rolling average, top-N by visit count. Ported into `stats.py` as pure data functions (no matplotlib).

Deliberate **changes** from the old code (all spec-mandated):

- ORES -> **Lift Wing outlink-topic-model** (D3): title-based POST, so the entire `titles_to_revids` step is deleted.
- The old `canonical_title()`/`url_to_title()` helpers were incomplete (no mobile-host handling, incomplete namespace list, no redirect resolution, inconsistent underscore handling). Replaced by the tested `normalize.py` (D5).
- UMAP is **fit once on the reference sample and frozen**; user articles are `.transform()`ed in (D4). The old code refit per-run, so layouts weren't comparable.
- File-based JSON/pickle caches -> DB cache tables (D6).
- HDBSCAN clustering, silhouette scoring, network analysis, and all plotting: **not ported** (Phase 2 or dropped).

## Implementation steps

Each step ends with a concrete verification. Steps follow the spec's ordering (S10).

### Step 1 - Scaffold + environment (D8)

- Create the repo layout from S4, plus `pageviews.py` and `data/` dirs (`data/` git-ignored except `.gitkeep`; `reference_code/` left untouched).
- `environment.yml` (conda, pinned): Python 3.11, `numpy` pinned to a numba-compatible version, `umap-learn`, `numba`/`llvmlite`, `sentence-transformers` (CPU torch), `pandas`, `requests`, `fastapi`, `uvicorn`, `pytest`, `httpx` (for TestClient), `tzlocal`. SQLite needs no dependency.
- `config.py`: DB path, `data/reference/` paths, `Api-User-Agent` string with contact email (`WikipediaWrapped/0.1 (emerjoe1779@gmail.com)`), timezone default (`tzlocal` detection with optional override constant), API base URLs, request throttle/backoff settings.
- **Verify:** `conda env create` succeeds; a smoke-test script imports `umap`, `numba`, `sentence_transformers`, `fastapi`, `sqlite3` (S9 requirement). Nothing else proceeds until this passes.

### Step 2 - `normalize.py` + tests (D5, highest risk, built first)

- `url_to_title(url) -> str | None`: pure function, no network. Handles `%`-encoding, `#fragment`, `?query`, underscores->spaces, mobile hosts (`en.m.wikipedia.org`), and returns `None` for non-article namespaces (`File:`, `Special:`, `Help:`, `Talk:`, `Category:`, `Template:`, `Portal:`, `Wikipedia:`, `Draft:`, `User:`, ...) and non-`/wiki/` paths.
- `resolve_redirects(titles) -> dict[str, str]`: batched MediaWiki Action API calls (`action=query&redirects=1&titles=...`, <=50 titles/request), also applying the API's `normalized` mapping. Network layer injected/mockable.
- `canonicalize(urls, cache) -> dict[url, str | None]`: composition used by ingest; consults the redirect cache first.
- **Verify:** `tests/test_normalize.py` covers every edge case in S7.1 with mocked API responses, plus one optional live-API integration test (marked, skippable offline). This module gets the densest tests in the repo because errors here silently drop data downstream.

### Step 3 - `extract/extract_history.py` (D1)

- Standalone script (stdlib-only, so it runs outside the conda env if needed).
- Two input modes: **Chrome History SQLite DB** (default: auto-locate per-OS - macOS `~/Library/Application Support/Google/Chrome/Default/History`, Windows `%LOCALAPPDATA%\Google\Chrome\User Data\Default\History`, Linux `~/.config/google-chrome/Default/History`; copy to temp file before reading) and **Takeout/extension JSON** (`--input file.json`, handling both the `{"Browser History": [...]}` wrapper and bare-list forms, and all three timestamp fields).
- Reads per-visit rows from Chrome's `visits` joined to `urls` (one row per visit, per D2 - not `last_visit_time`, which collapses to one row per URL).
- Filters to Wikipedia hosts; normalizes timestamps to UTC ISO-8601; emits the S5 schema (`schema_version: "1.0"`) to `--output`.
- **Verify:** run against the sample exports in `reference_code/` (e.g. `history.json`, `WikipediaHistory.json`) and confirm valid schema output; unit tests for both timestamp conversions.

### Step 4 - `storage.py` + `ingest.py` (D2, D6)

- `storage.py`: schema DDL for the S6 tables (`visits`, `pages`, `topic_cache`, `pageview_cache`, `embedding_cache`, `umap_coords`) plus thin CRUD/upsert helpers. Embedding vectors stored as BLOBs (`np.float32.tobytes()`), model name alongside. All SQL lives here only.
- `ingest.py`: validate `schema_version` -> `url_to_title` filter -> batch redirect resolution (cached) -> upsert `pages` (canonical title, `raw_title`, `first_seen`) -> insert `visits` keyed to page ids. Idempotent: re-ingesting the same file or an updated export doesn't duplicate visits (dedupe on `(page_id, timestamp)`).
- **Verify:** ingest a real extract twice; row counts identical after the second run; unique-page vs total-visit counts match a hand-checked pandas computation.

### Step 5 - `stats.py` (D2)

All functions take a DB connection (+ timezone parameter) and return plain data structures (dicts/lists - JSON-ready). Frequency/time stats over **visits**; "pages visited" counts unique **pages** (D2).

- `summary()`: unique pages, total visits, date range.
- `most_visited(n)`: top pages by visit count.
- `weekday_hour_grid(tz)`: 7x24 counts, computed in the given IANA zone (device default), returned raw - density normalization is presentation-layer.
- `daily_series(tz)`: visits per local calendar day + 7-day rolling average.
- `busiest_week(tz)`: max-visit ISO calendar week.
- `topic_distribution()`: fraction of unique pages per macro-topic (and raw label), joining `pages` x `topic_cache`.
- **Verify:** `tests/test_stats.py` against a small fixture DB with hand-computed expected values, including a DST-crossing timestamp to prove IANA-zone correctness.

### Step 6 - `topics.py` (D3)

- Lift Wing client: `POST /service/lw/inference/v1/models/outlink-topic-model:predict` with `{"page_title": ..., "lang": "en"}`, real `Api-User-Agent`. One title per request (the model API is per-title), modest throttle, exponential backoff on 429/5xx.
- `macro()` ported unchanged. Store `(canonical_title, macro_topic, raw_label, fetched_at)` in `topic_cache`; only fetch titles not already cached (D6). Titles the model can't score -> stored as unclassified so they aren't re-fetched every run.
- **Verify:** unit tests with mocked responses (incl. 429 backoff and missing-page handling); a small live smoke test against a few known titles.

### Step 7 - `embeddings.py`

- Summary fetch (REST `page/summary/{title}`, description + extract concatenated; 404 -> empty payload recorded so it isn't retried forever) -> `all-MiniLM-L6-v2` batch encode, `normalize_embeddings=True`.
- Cache in `embedding_cache` keyed by `(canonical_title, model_name)`; only encode what's missing (D6).
- **Verify:** unit test that cached titles skip both fetch and encode; vector shape (384) and unit norm asserted.

### Step 8 - `scripts/build_reference_space.py` + `umap_space.py` (D4)

- **Build script (one-time / refreshable):**
  1. Head: top 30k `en` titles from a pageviews dump hour-file (the local copy `reference_code/pageviews-202505.gz` works as default input; `--dump-url` to refresh), excluding non-article namespaces.
  2. Tail: 10k random mainspace titles via `list=random`.
  3. Label all ~40k via `topics.py` (shared cache), for coverage reporting and stored per-point topic metadata.
  4. Fetch summaries + embed via `embeddings.py` (shared cache - interrupted runs resume for free).
  5. Fit UMAP (`n_neighbors=30, min_dist=0.1, metric="cosine", random_state=42`) on the reference embeddings.
  6. Persist to `data/reference/`: fitted reducer (pickle + a metadata JSON recording model name, sample composition, UMAP params, build date), reference embeddings (`.npy`), reference titles/topics/coords (parquet or JSON).
- **`umap_space.py` (per-run):** load the frozen reducer, `.transform()` visited-article embeddings, upsert `umap_coords`. Never refits; raises clearly if artifacts are missing (pointing at the build script). Skips titles already in `umap_coords`.
- Long-running steps checkpoint via the DB caches; the script is resumable and reports per-step progress.
- **Verify:** build on a `--limit`-reduced sample first (fast end-to-end test); assert `transform()` of a held-out reference point lands near its original fitted coordinate; then run the full 40k build.

### Step 9 - `pageviews.py` (D7, client + cache only)

- `GET /metrics/pageviews/per-article/en.wikipedia/all-access/user/{title}/daily/{start}/{end}`, where `{start}`/`{end}` come from the user's visit date range (D7). Sum daily to `total_views`; cache by `(canonical_title, date_start, date_end)` (D6). Handle 404 (no data) as zero-with-flag.
- Comparison analytics (most/least popular-with-others, profile alignment) are **not** built - Phase 2.
- **Verify:** mocked unit tests + live smoke test on one title.

### Step 10 - `api.py` (end of Phase 1)

Thin FastAPI app; all logic stays in the modules. Endpoints:

- `GET /health` - DB reachable, reference artifacts present y/n.
- `POST /ingest` - upload/point at a standardized JSON; runs ingest; returns counts.
- `POST /enrich` - runs topics + embeddings + UMAP projection (+ optionally pageviews) for un-enriched pages; returns progress summary. (Split from ingest so slow network enrichment is explicit and re-runnable.)
- `GET /stats/summary`, `/stats/top?n=`, `/stats/heatmap?tz=`, `/stats/daily?tz=`, `/stats/busiest-week?tz=`, `/stats/topics` - `tz` optional, defaulting to device zone.
- `GET /map` - visited pages with `{title, x, y, macro_topic, visit_count}` (the Phase 2 scatter payload).
- `GET /pages/{title}` - per-page detail: visits, topic, pageviews if cached (the Phase 2 mouseover payload).
- **Verify:** `pytest` + `TestClient` endpoint tests against a fixture DB; then a live end-to-end run - extract real history -> ingest -> enrich -> curl each endpoint - as the Phase 1 acceptance check.

## Testing & verification strategy

- **Unit tests** (offline, mocked network): `normalize` (densest), `stats` (fixture DB + DST case), extractor timestamp conversions, cache-hit behavior in `topics`/`embeddings`/`pageviews`, API endpoints.
- **Marked integration tests** (live network, skippable): one per external API.
- **End-to-end acceptance:** full pipeline on Joe's real history via the API, verifying counts, a labeled topic distribution, and populated `umap_coords`.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `umap-learn`/`numba`/`llvmlite`/NumPy version conflicts | Pinned known-good combo in `environment.yml`; import smoke test is step zero (S9); nothing proceeds until it passes. |
| Lift Wing labels differ from old ORES labels | Expected per spec S8 - same taxonomy, so `macro()` applies; no old-label compatibility attempted. |
| 40k-article reference build is slow / interruptible | All fetch/embed steps checkpoint through DB caches; script is resumable; `--limit` mode for fast pipeline testing before the full run. |
| Wikimedia rate limits (429) | Shared HTTP helper: throttle + exponential backoff + mandatory `Api-User-Agent`. |
| Redirect-resolution errors silently dropping data | Built and tested first; ingest logs URLs that normalize to `None` and titles that fail resolution, so drops are visible, not silent. |

## Explicitly out of scope (Phase 2)

Frontend/UI, popularity-comparison analytics, network/graph analysis, HDBSCAN clustering, plotting of any kind, automatic in-browser capture.
