# WikiWrapped — Build Specification

**Purpose of this document.** This is a starting brief for Claude Code to scaffold a new, standalone repository. It resolves the key design decisions so the agent does not have to guess. It is intentionally opinionated: where a choice was made, it is stated as a requirement, not an option. Prior exploratory scripts exist and can be read for reference/logic, but **this is a fresh repo** — do not attempt to make the old scripts "work"; port their logic into clean modules.

---

## 1. What we're building

A personal analytics application for a user's own Wikipedia reading history. The user exports their browser history, the app extracts Wikipedia visits, analyzes them, and presents interactive visualizations.

The pipeline, end to end:

1. **Extract** — a standalone script turns a raw browser history into a standardized JSON file.
2. **Ingest & normalize** — the app loads that JSON, canonicalizes URLs to Wikipedia article titles (resolving redirects), and stores visits/pages in a local database.
3. **Analyze** — compute basic stats, topic labels, and an embedding map.
4. **Serve & visualize** — a backend serves computed results; a web frontend (later phase) renders multi-panel interactive visuals.

### Build order (important)

- **Phase 1 (this spec's focus): backend only.** Everything through a queryable API/data layer, verified via scripts or a minimal endpoint check. No polished frontend yet.
- **Phase 2:** web frontend (multi-panel interactive UI), popularity comparison, and network/graph analysis.

The architecture must be **modular** so Phase 2 features bolt on without rewrites.

---

## 2. Non-negotiable design decisions (already resolved)

These were decided deliberately. Do not deviate without flagging.

| # | Decision |
|---|----------|
| D1 | **Canonical input format.** The extraction script is the only thing that touches browser-specific formats. It emits one standardized JSON schema (see §5). The app *only* ingests that schema — it never parses raw browser DBs or Takeout files directly. |
| D2 | **"Visit" vs "Page" are distinct.** A **visit** = one page entry with a timestamp. A **page** = one unique Wikipedia article. "Number of pages visited" = count of unique pages. All frequency/time-based stats (streaks, day-of-week, hour-of-day, busiest week) are computed over **visits**. |
| D3 | **Topic classification uses Lift Wing, not ORES.** ORES is deprecated. Use the outlink topic model (see §8). It takes a page title directly — no revision-ID lookup step. |
| D4 | **UMAP space is fixed and reproducible.** Fit UMAP once on a large, topic-representative reference sample of Wikipedia articles. Persist the fitted reducer + reference embeddings. Visited articles are projected via `.transform()` into that frozen space. This only need to be computed once. |
| D5 | **Redirect resolution is its own tested module.** URL→title normalization (including redirect resolution) is the join key for topics *and* pageview stats; errors silently drop data downstream. It gets dedicated, tested code. |
| D6 | **Caching is first-class.** Embeddings, topic labels, and pageview counts are cached in the database keyed by title (+ date range where relevant). Re-running on an updated history only fetches what's new. |
| D7 | **Pageview popularity is matched to the user's date range.** "How popular was this page with other readers" is measured over the same date range covered by the user's history, not all-time. |
| D8 | **Python core, conda environment.** Core in Python. Dependencies in a pinned `environment.yml`. Building the env is step zero and must be verified before anything else (see §9). |
| D9 | **The append-only log is the source of truth.** `data/visits.jsonl` holds one JSON record per raw visit (`url`, `title`, `timestamp`, `source`, `account`), is only ever appended to, and is deduped on read. `data/app.db` is a **derived, rebuildable** artifact. This exists because Chrome expires local history after ~90 days: whatever reached the log before the window rolled is the only surviving copy, so the log is the thing worth backing up. Every input source writes here first, and the DB is updated from it second. **Amended 2026-10-24:** the log, the DB, sync state, settings and tokens live in the per-user data directory (`config.USER_DATA_DIR`), not the repo; the program folder is read-only at runtime. Paths below that say `data/` mean `<user data>/data/`. Repo-local copies are migrated only by the explicit, copy-only `python -m app.migrate`. |
| D10 | **Google Takeout is an input source alongside local Chrome, never a replacement.** Takeout exports at the *account* level (Chrome Sync has already merged every device), so it reaches past the 90-day limit and covers a second account and mobile. Drive access is **read-only** (`drive.readonly`) and the app never handles a Google password: an OAuth token is the only credential it holds. One token file per account under `tokens/` (in the user-data dir since 2026-10-24). Provenance lives in the `visit_sources` join table, not columns on `visits`, because one physical visit can legitimately arrive from both routes. The local-Chrome path must keep working with this disabled. **Amended 2026-10-24:** the default Takeout route is a *watched folder*: the user picks local folders (filled by hand or by Google Drive for desktop) and each sync ingests any new `takeout*.zip` found there, read in place, with no Google sign-in. Those visits are logged as source `takeout` with the folder's label as the account. The OAuth/Drive route above remains as an optional Advanced path. |
| D11 | **Cross-source dedup is measured, not assumed.** The same visit can arrive from both routes, and quiet duplicates would inflate every statistic. Dedup keys on the **canonical post-redirect title** (D5) plus a timestamp tolerance held in one constant, `app/dedup.py:DEDUP_TOLERANCE_S`. It ships at `0` (exact-second match). Raise it only on evidence from `scripts/compare_sources.py` run against a real Takeout archive; every second of slack risks merging genuine rapid revisits. |
| D12 | **Check-on-launch sync, not a daemon.** `run.sh` runs `python -m app.sync --on-launch` as a separate process before uvicorn. No scheduler, no background process, and no import-time side effects in `app.api`. The sync **must never block or break startup**: every expected failure is caught, written to `data/sync_state.json`, surfaced on `GET /health`, and the dashboard serves existing data regardless. **Amended 2026-10-24:** `app/launcher.py` replaces `run.sh` (now a thin wrapper). The sync and enrichment still run as separate processes, but as background jobs started *after* the pywebview window opens, with progress shown in the window (`app/jobs.py`, `app/progress.py`); closing the window terminates them. Because a sync now writes while the API reads, the database runs in WAL mode. Still no daemon, no scheduler, and no import-time side effects in `app.api`. |

---

## 3. Architecture overview

```
┌─────────────────┐     standardized      ┌──────────────────────────────┐
│  extract script │ ───── JSON file ────▶ │        backend app           │
│  (standalone,   │      (upload)         │                              │
│   run locally)  │                       │  ingest → normalize →        │
└─────────────────┘                       │  store → analyze → serve     │
                                          │                              │
                     external APIs ◀──────┤  (Lift Wing, Pageviews,      │
                     (cached)             │   MediaWiki redirects)       │
                                          └──────────────┬───────────────┘
                                                         │
                                              ┌──────────▼───────────┐
                                              │  local DB (SQLite/   │
                                              │  DuckDB): visits,    │
                                              │  pages, caches       │
                                              └──────────┬───────────┘
                                                         │
                                              ┌──────────▼───────────┐
                                              │  API (FastAPI)       │
                                              │  → frontend (Phase 2)│
                                              └──────────────────────┘
```

- **Backend framework:** FastAPI (async, clean JSON endpoints, plays well with a JS frontend later).
- **Storage:** SQLite or DuckDB (single-file, zero-server). DuckDB is preferable if analytical/aggregate queries dominate; SQLite is fine and simplest. Pick one, document why, keep the data-access layer thin so it could be swapped.
- **Frontend (Phase 2):** FastAPI backend + JS frontend (React + Plotly.js/D3). Chosen over Streamlit because the target UX (mouseover popups with per-article stats, related-article links, external hyperlinks) exceeds what Streamlit does comfortably. Not in scope for Phase 1.

---

## 4. Suggested repo layout

```
wikiwrapped/
├── environment.yml
├── README.md
├── config.py                 # paths, API user-agent, date-range defaults
├── extract/
│   └── extract_history.py     # standalone; browser DB/Takeout → standardized JSON
├── app/
│   ├── ingest.py              # load standardized JSON → DB (visits, pages)
│   ├── normalize.py           # URL → canonical title (D5), incl. redirect resolution
│   ├── topics.py              # Lift Wing outlink-topic-model client + macro() collapsing
│   ├── stats.py               # basic stats (returns data, not plots)
│   ├── embeddings.py          # summary fetch + sentence-transformer encode
│   ├── umap_space.py          # build/persist reference space; project visits (D4)
│   ├── pageviews.py           # per-article pageview API client (D7)
│   ├── storage.py             # DB schema + thin data-access layer (+ cache tables, D6)
│   └── api.py                 # FastAPI app exposing computed results
├── data/
│   ├── reference/             # persisted UMAP model + reference embeddings
│   └── app.db                 # local database
├── scripts/
│   └── build_reference_space.py   # one-time: sample articles, embed, fit+save UMAP
└── tests/
    └── test_normalize.py      # redirect + URL edge cases (mobile host, fragments, %-enc)
    └── test_stats.py          # unit tests for basic stats
```

---

## 5. Standardized input schema (extractor output)

The extraction script produces a single JSON file. Proposed schema:

```json
{
  "schema_version": "1.0",
  "source_browser": "chrome",
  "exported_at": "2026-07-19T00:00:00Z",
  "visits": [
    {
      "url": "https://en.wikipedia.org/wiki/Douglas_Adams",
      "title": "Douglas Adams",
      "timestamp": "2025-05-01T14:32:00Z"
    }
  ]
}
```

Requirements for the extractor:
- Handles the timestamp conventions in play and **normalizes all to UTC ISO-8601**:
  Chrome SQLite `visits.visit_time` = us since **1601**; Takeout `time_usec` =
  us since **1970** (a string); extension `visitTime`/`lastVisitTime` = ms since 1970.
  **Corrected 2026-08-09 against a real archive:** Takeout `time_usec` is NOT the
  1601 epoch, contrary to what the old reference scripts assumed. Reading it that
  way puts every row in 1663 BC, where the plausibility check drops it, turning
  2804 real Wikipedia visits into zero with no error. `usec_to_unix` now decodes
  both readings and keeps the plausible one rather than hardcoding either.
- **Cross-platform history-file location** (the old script hardcodes a macOS path — fix this: support macOS/Windows/Linux Chrome default profile paths, with an explicit `--input` override).
- Copies the SQLite `History` DB to a temp file before reading (it's locked while Chrome runs) — the old code already does this; keep it.
- Filters to Wikipedia hosts only.
- Emits the schema above. Nothing downstream should ever need to know which browser it came from.

The app validates `schema_version` on ingest.

---

## 6. Data model (DB)

Minimum tables:

- **`visits`** — `(id, page_id, timestamp)`. One row per visit (D2).
- **`pages`** — `(id, canonical_title, raw_title, first_seen, visit_count)`. One row per unique article (D2). `canonical_title` is post-redirect-resolution (D5).
- **`topic_cache`** — `(canonical_title, macro_topic, raw_label, fetched_at)`. (D6)
- **`pageview_cache`** — `(canonical_title, date_start, date_end, total_views, fetched_at)`. Keyed on date range (D6, D7).
- **`embedding_cache`** — `(canonical_title, vector, model_name, fetched_at)`. (D6)
- **`umap_coords`** — `(canonical_title, x, y)` for visited pages projected into the frozen space (D4).

Reference-space artifacts (fitted UMAP model, reference embeddings, reference coords) live as files under `data/reference/`, not in the DB. Since 2026-10-24 they are a read-only app resource resolved by `config.resource_dir()` (env override, then the installed app, then the repo's `data/reference/`).

---

## 7. Module contracts

### 7.1 `normalize.py` (D5) — highest-risk module, build & test first
- **Input:** a Wikipedia URL. **Output:** canonical article title (or `None` if not an article).
- Must handle: `%`-encoding, `#section` fragments, `?query` params, underscores vs spaces, **mobile host** (`en.m.wikipedia.org`) vs desktop, and non-article namespaces (`File:`, `Special:`, `Help:`, `Talk:` → `None`).
- Must resolve **redirects** to the canonical article (MediaWiki API `redirects` param), because pageview stats and topic labels are keyed on the canonical title.
- Batch redirect lookups; cache results.
- Ships with `tests/test_normalize.py` covering each edge case above.

### 7.2 `topics.py` (D3)
- Client for Lift Wing outlink-topic-model (§8). Input: canonical title. Output: top macro-topic label.
- Port the `macro()` collapsing logic (first-level topic, except keep `Culture.*` at second level) unchanged.
- Respect caching (D6) and rate/etiquette rules (§8).

### 7.3 `stats.py` (D2)
Functions return **data structures, not matplotlib figures** (plotting happens in the frontend/Phase 2). Compute:
- Unique pages visited (count of `pages`).
- Total visits.
- Most-visited page (by visit count).
- Day-of-week × hour-of-day frequency grid (over visits; timezone-aware — old code converts to local time, make the offset configurable).
- Reading streaks: visits-per-day time series (+ rolling average).
- Busiest week (max visits in a calendar week).
- Category distribution: fraction of unique pages per macro-topic and subcategory.

### 7.4 `embeddings.py`
- Fetch summary (description + extract) via Wikipedia REST summary endpoint.
- Encode with `sentence-transformers/all-MiniLM-L6-v2`, normalized embeddings.
- Cache vectors (D6).

### 7.5 `umap_space.py` + `scripts/build_reference_space.py` (D4)
- `build_reference_space.py` (one-time / refreshable): sample a large, topic-representative set of high-quality Wikipedia articles (port the frequency- + category-stratified sampling logic from the old sampler), embed them, **fit** UMAP, and **persist** the reducer + reference embeddings + reference coords to `data/reference/`.
- `umap_space.py` (per-run): load the frozen reducer and `.transform()` the user's visited-article embeddings into the same space. Never refit. Store results in `umap_coords`.
- Note the reference sample won't contain the user's exact articles — that's expected and acceptable; the frozen transform is what guarantees reproducibility.

### 7.6 `pageviews.py` (D7) — supports Phase 2 comparisons
- Client for the per-article Pageviews REST API (§8), queried over the user's history date range.
- Powers: most/least popular-with-others among visited pages; profile-vs-popular comparisons.
- Cache by `(title, date_start, date_end)`.

### 7.7 `api.py`
- FastAPI endpoints returning the computed stats/coords/topics as JSON. Thin — all logic lives in the modules above. This is the seam the Phase 2 frontend consumes.

---

## 8. External APIs — reference

**Lift Wing outlink topic model** (replaces ORES):
```
POST https://api.wikimedia.org/service/lw/inference/v1/models/outlink-topic-model:predict
Content-Type: application/json
Api-User-Agent: WikiWrapped/0.1 (https://github.com/emers245/wikiwrapped)

{"page_title": "Douglas_Adams", "lang": "en"}
```
- Title-based; **no revision-ID lookup needed** — delete the old `titles_to_revids` step.
- It is an *outlink*-based model, so labels differ slightly from the old text-based `articletopic`; the topic taxonomy (WikiProject scheme) is the same, so `macro()` still applies.
- **Set a real `Api-User-Agent` header with contact information** — Wikimedia enforces this. **Amended 2026-10-24:** the header identifies the project, `WikiWrapped/<version> (https://github.com/emers245/wikiwrapped)`, which Wikimedia's policy accepts (a URL or an email); no user has to supply a personal address. An optional `WIKIPEDIA_WRAPPED_EMAIL` is appended when set.
- Anonymous rate limit ≈ 50,000 requests/hour per IP (no auth token needed for personal use). Batch politely; back off on HTTP 429.

**Pageviews REST API** (per-article, cheaper than raw dumps for this use):
```
GET https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/
    en.wikipedia/all-access/user/{title}/daily/{start}/{end}
```
- Query over the user's history date range (D7). Same `Api-User-Agent` etiquette.

**Wikipedia REST summary** (for embeddings text):
```
GET https://en.wikipedia.org/api/rest_v1/page/summary/{title}
```

**MediaWiki Action API** (redirect resolution in `normalize.py`):
use `action=query&redirects=1&titles=...` (batchable).

---

## 9. Environment & dependencies (D8)

- Deliver a **pinned** `environment.yml`. Building it is **step zero** and must succeed before other work.
- **Known pain point:** `umap-learn` pulls `numba`/`llvmlite`, which are strict about NumPy/Python versions. Pin a known-good combination and verify import of `umap`, `numba`, `sentence_transformers`, `fastapi`, and the DB driver in a smoke test before proceeding.
- Core libraries: `pandas`, `requests`, `sentence-transformers`, `umap-learn`, `scikit-learn`, `fastapi`, `uvicorn`, `duckdb` **or** `sqlite` (per §3), plus `pytest`.

---

## 10. Phased implementation plan (hand to Claude Code in order)

1. **Scaffold + env.** Repo, `environment.yml`, module skeleton (§4), config. Verify env builds (§9).
2. **`normalize.py` + tests.** Build the riskiest join-key module first (D5).
3. **`extract_history.py`.** Standalone extractor → standardized JSON (§5), cross-platform.
4. **`ingest.py` + `storage.py`.** Load JSON, populate `visits`/`pages`, set up cache tables (D2, D6).
5. **`stats.py`.** Basic stats returning data structures (D2).
6. **`topics.py`.** Lift Wing labels + caching (D3).
7. **`build_reference_space.py` + `umap_space.py`.** Frozen reference space; project visits (D4).
8. **`api.py`.** Expose everything as JSON. End of Phase 1.
9. **Phase 2:** frontend (multi-panel, mouseover popups w/ per-article stats + links), `pageviews.py` popularity comparisons (D7), and network/graph analysis.

---

## 11. Deferred to Phase 2 / notable non-goals

- **Network/graph visualization:** wanted, but note **Gephi cannot embed in a web page** — it's a separate desktop app. Use it for one-off exploratory analysis while developing graph metrics (in-degree/out-degree, PageRank), but the shipped in-app network panel should use a JS graph library (**sigma.js** — what Gephi's own web export uses — or cytoscape.js/D3).
- **Popularity/profile alignment** (most/least popular-with-others, category-vs-popular, embedding-space alignment to the popular-page profile): depends on `pageviews.py`; Phase 2.
- **Automatic in-browser history capture:** out of scope. The extractor script + upload flow is the deliberate boundary (D1).

---

## 12. Open questions to confirm before/early in the build

- Reference-sample size and refresh cadence for the UMAP space (bigger = better coverage, slower to build).
- Timezone handling: fixed offset (old code used UTC-6) vs. detected/configurable. Recommend configurable, default from history data if inferable.
- DuckDB vs SQLite final call (§3).
