# Wikipedia Wrapped - Phase 2 Implementation Plan

Web-based interactive visualization served by the existing FastAPI backend.
One main view with two modes (UMAP map / reading network), hover-linked
detail popups, and three statistics panels along the bottom.

## Resolved decisions (confirmed with Joe, 2026-07-19)

| Question | Decision | Rationale |
|---|---|---|
| Frontend stack | **Vanilla HTML/JS/CSS, no build step**; libraries from CDN; FastAPI serves the static files | Single-page personal app; avoids installing a node/npm toolchain; easiest to debug and iterate. (Deviation from repo_spec S3's React suggestion, approved.) |
| Category-chart baseline | **Top-viewed head sample only**, labeled "popular pages" | Compares Joe's reading against what other readers are reading, not against all of Wikipedia. Head fractions come from the reference build's labeled points. |
| Network-mode layout | **Force-directed** (cytoscape cose) | Hubs become visually central; the two modes are genuinely different views. |
| UMAP backdrop | **Faint reference cloud behind user pages** | Reading in context, like qc/umap_check_projection.png; backdrop is non-interactive. |

Prior decisions that carry over: colors keyed to macro topic consistently
across map, network, and bar chart; device timezone for time panels;
network edges = session transitions (15-min gap) from Phase 1's
`app/network.py`.

## Architecture

```
Browser (vanilla JS)
  index.html + style.css + main.js
  |- map.js      UMAP mode: D3 (canvas backdrop + SVG user nodes)
  |- network.js  network mode: cytoscape.js (already proven in Phase 1)
  |- panels.js   bottom panels: D3 (bars, daily line, hour-of-day)
        |
        v  one fetch at load
GET /dashboard   <- new aggregate endpoint (FastAPI, thin)
GET /            <- serves index.html; /static/* for assets
```

**Why one aggregate endpoint:** hover popups and panel highlighting must be
instant, so all data loads once at startup instead of per-hover requests.
At personal scale (~257 pages, ~1000 visits, ~2k-40k backdrop points) the
payload is fine. Existing granular endpoints stay untouched.

## Step 1 - Backend additions

New `GET /dashboard` composing existing module functions (no new logic,
staying thin per S7.7):

- `pages`: per page - title, visit_count, first_seen, macro_topic, x, y,
  in_degree, out_degree, pagerank, pageviews (cached total), and the list
  of visit timestamps (needed for the hover-linked time-panel markers).
  Article URL is constructed client-side from the title.
- `edges`: from `network.graph_metrics` (weights included).
- `reference`: backdrop points {x, y, macro_topic} from
  `umap_space.load_points`, plus the head-sample topic fractions for the
  baseline bars (computed here from the same points, `source == "head"`).
- `stats`: `daily_series`, `weekday_hour_grid` (client sums columns for
  the hour-of-day panel), `topic_distribution`, `summary`.

Also: `GET /` returns index.html; mount `app/static/` via StaticFiles.
Unit tests: /dashboard shape against a seeded fixture DB + fixture
reference dir; / returns HTML.

Open item: when the full 40k reference build lands, the backdrop payload
grows to ~2 MB. Plan: `?backdrop_limit=` with a default around 10k,
random-downsampled server-side (backdrop is decorative; 10k points render
fine on canvas).

## Step 2 - Page skeleton, layout, theme

- CSS grid: header band (logo "Wikipedia Wrapped" top-left + mode toggle
  UMAP/Network + summary line), main viz area (~70% height), bottom band
  of three equal panels (categories | daily | time-of-day).
- Theme: light off-white background (#faf9f6), dark-gray text, one muted
  categorical palette (tab10-derived, desaturated) defined once in JS and
  used for topics everywhere; grays for de-emphasis. No abrasive colors.
- Loading state while /dashboard fetches.

## Step 3 - UMAP mode (D3)

- Canvas layer: reference backdrop, very light gray-tinted dots,
  non-interactive.
- SVG layer: user pages as circles, radius scaled by visit_count (sqrt),
  colored by macro topic; d3.zoom pan/zoom applied to both layers in sync.
- Hover: node enlarges (transition), popup box near cursor with core
  stats (visits, topic, pageviews-by-others, first seen, in/out-degree,
  PageRank) and an "open on Wikipedia" link; all other nodes drop to low
  opacity. Popup stays open while the cursor is over it so the link is
  clickable.

## Step 4 - Network mode (cytoscape.js)

- Same data, force-directed (cose) layout, node size by PageRank, same
  topic colors, edge width by transition weight; built-in wheel zoom/pan.
- Hover: node enlarges + same popup; its edges and neighbor nodes get a
  highlight class while everything else fades (cytoscape style classes) -
  connections can be followed hop by hop.
- Mode toggle swaps which layer is visible; both initialize once and keep
  their zoom state.

## Step 5 - Bottom panels (D3)

- **Categories (left):** grouped bar chart per macro topic - "you"
  (fraction of your unique pages) vs "popular pages" (head-sample
  fraction), topic-colored with the baseline bar hatched/lightened.
- **Daily (middle):** visits-per-day line with 7-day rolling average,
  from `daily_series`.
- **Time of day (right):** visits per hour 0-23 (grid columns summed),
  device timezone noted in the axis label.

## Step 6 - Hover linking across panels

On node hover (either mode), a single `highlight(page)` event:

- Categories: the hovered page's topic pair stays full-strength, other
  bars fade.
- Daily + time-of-day: faded base series + vertical marker lines at the
  page's visit dates / visit times (from the per-page timestamps in the
  dashboard payload).
- Mouseleave restores everything (single `clear()` path so states cannot
  stick).

## Step 7 - Polish + verification

- Empty-state handling (no coords yet -> prompt to run /enrich; no
  reference dir -> UMAP mode disabled with a note).
- Verification: unit tests for /dashboard; manual browser pass of the
  checklist (mode toggle, zoom, hover popup + link, neighbor highlight,
  panel fade/markers, resize); screenshot review against this spec.

## Risks / notes

- Hover jitter on dense clusters: use a small hover hit-radius
  (d3.quadtree nearest-point) rather than exact circle hit-testing.
- cytoscape cose layout on 257 nodes is fast; if the graph grows, switch
  to fcose (CDN plugin).
- The interim 1989-point reference space drives both backdrop and
  baseline until the full 40k build runs; fractions will shift slightly
  then.
- Popup URL: `https://en.wikipedia.org/wiki/<title with underscores>`,
  percent-encoded.
```

Estimated order of work: Step 1 (backend + tests) first and alone, then
2-4 as one reviewable chunk (the main view), then 5-6 (panels + linking),
then 7.
