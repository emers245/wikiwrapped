# wikipedia-wrapped

A visualization tool for personalized Wikipedia reading history: export your
browser history, extract the Wikipedia visits, and explore stats, topics, and
an embedding map of what you read.

Specification: `repo_spec.md` | Phase 1 plan: `claude_phase1.md`

## Setup

Building the conda environment is step zero (repo_spec S9) - verify it before
anything else:

```bash
conda env create -f environment.yml
conda activate wikipedia-wrapped
python scripts/check_env.py --full
```

Wikimedia APIs require a contact email in the User-Agent header. Provide
yours one of two ways (it is never committed to git):

```bash
export WIKIPEDIA_WRAPPED_EMAIL="you@example.com"   # environment variable, or:
echo "you@example.com" > contact_email.txt          # git-ignored file at repo root
```

## Usage (Phase 1 pipeline)

```bash
# 1. Extract Wikipedia visits from your browser history (stdlib-only)
python extract/extract_history.py --output wikipedia_history.json

# 2. One-time: build the frozen UMAP reference space (hours at full scale;
#    resumable; use --head/--tail for small test builds)
python scripts/build_reference_space.py

# 3. Serve
uvicorn app.api:app --reload
```

Then drive it over HTTP:

```bash
curl -X POST localhost:8000/ingest --json @wikipedia_history.json
curl -X POST "localhost:8000/enrich?include_pageviews=true"
curl localhost:8000/stats/summary
```

Endpoints: `/health`, `POST /ingest`, `POST /enrich`, `/stats/summary`,
`/stats/top?n=`, `/stats/heatmap?tz=`, `/stats/daily?tz=`,
`/stats/busiest-week?tz=`, `/stats/topics`, `/map`, `/network`,
`/pages/{title}`, `/dashboard`. Interactive docs at `localhost:8000/docs`.
Run tests with `pytest` (`-m "not integration"` to stay offline).

## Dashboard (Phase 2)

Open `localhost:8000/` in a browser for the interactive dashboard. It is
a single page (vanilla JS, no build step; D3 + cytoscape.js from CDN,
served from `app/static/`) that loads everything once from
`GET /dashboard` so all hover interactions are instant.

- **Main view, two modes** (toggle top-left):
  - *UMAP*: your pages positioned in the frozen semantic space, sized by
    your visit count, colored by macro topic, over a faint backdrop of
    the reference articles. Scroll to zoom, drag to pan.
  - *Network*: the reading-session graph (visits to two articles within
    15 minutes form an edge), force-directed, node size = PageRank.
    Hovering highlights a node's connections and fades the rest so paths
    can be followed.
- **Hover** any node: it enlarges and a popup shows its core stats
  (your visits, topic, views by other readers, in/out-degree, PageRank)
  with a link to the article. The bottom panels simultaneously fade to
  highlight that page: its topic's bars in the category chart, and
  vertical markers at its visit dates/times on the time panels.
- **Bottom panels**: category fractions (you vs the top-viewed "popular
  pages" sample from the reference build), visits per day with 7-day
  rolling average, and visits by hour of day (device timezone).

The UMAP mode and the "popular" baseline require the reference space
(`data/reference/`) and enriched pages (`POST /enrich`); without them the
app falls back to network mode only.

## Layout

```
config.py                    # paths, API user-agent, timezone, request policy
extract/extract_history.py   # standalone: browser history -> standardized JSON
app/                         # ingest, normalize, storage, stats, topics,
                             # embeddings, umap_space, pageviews, api
scripts/                     # build_reference_space.py, check_env.py
data/                        # local DB + reference artifacts (git-ignored)
tests/                       # pytest suite
reference_code/              # prior exploratory scripts (read-only reference)
```
