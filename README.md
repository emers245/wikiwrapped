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

# 3. Serve + open the dashboard
./run.sh
```

`run.sh` is the whole launch procedure in one command: it locates the conda
environment by path (so no `conda activate` is needed first), starts
uvicorn, waits for the API to answer, opens the dashboard in a browser, and
leaves the server in the foreground so Ctrl-C stops it. Re-running it while
a server is already up on that port reuses it instead of failing to bind.

```bash
./run.sh --port 8080     # serve somewhere else
./run.sh --no-browser    # server only
```

To launch from anywhere, add an alias to `~/.zshrc`:

```bash
alias wikiwrapped="$HOME/Documents/GitHub/wikipedia-wrapped/run.sh"
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
  with a link to the article. The side panels simultaneously fade to
  highlight that page: its topic's bars in the category chart, and
  vertical markers at its visit dates/times on the time panels.
- **Side panels**: category fractions (you vs the top-viewed "popular
  pages" sample from the reference build), visits per day with 7-day
  rolling average, and visits by hour of day (device timezone).
  They sit in a left sidebar that the **Panels** button in the header
  stows and restores, so the map or network can have the full window.
  The choice is remembered across reloads (`localStorage`), and below a
  720px viewport the sidebar floats over the visualization as a drawer
  instead of taking its own column.

The UMAP mode and the "popular" baseline require the reference space
(`data/reference/`) and enriched pages (`POST /enrich`); without them the
app falls back to network mode only.

### Frontend layout contract

The page is a two-column CSS grid: a full-width header over a sidebar
column and the visualization. The sidebar's width lives on the sidebar
element rather than in `grid-template-columns`, because an `auto` grid
column follows an animated element width in every browser while an
animated grid template does not.

Every view measures itself from its live container box and none of them
notice a CSS-driven resize on their own, so each module exposes a
`relayout()` that `main.js` calls from one debounced handler:

| module       | `relayout()` does                                     |
|--------------|-------------------------------------------------------|
| `panels.js`  | full D3 redraw (charts are sized from the SVG box)     |
| `map.js`     | recomputes the equal-aspect scales and redraws         |
| `network.js` | `cy.resize()` (cytoscape caches its container size)    |

Add a view and it needs a `relayout()` too, or it will render at a stale
width whenever the sidebar is toggled. The toggle redraws once the slide
has settled; `SLIDE_MS` in `main.js` must match the `#panels` width
transition in `style.css`.

The category chart is drawn with horizontal bars specifically so topic
names stay readable in a narrow column, where rotated labels under
vertical bars did not.

Chart type sizes are the `AXIS_FONT` / `LABEL_FONT` / `LEGEND_FONT`
constants at the top of `panels.js` (SVG text cannot be styled from
`style.css` the way the panel headings are). The `MARGIN` and `CAT_MARGIN`
objects beside them reserve room for that text, so raising a font size
means raising the matching margin, and `--panel-w` in `style.css` sets how
much width the labels have to fit in.

## Layout

```
run.sh                       # one-command launcher (server + browser)
config.py                    # paths, API user-agent, timezone, request policy
extract/extract_history.py   # standalone: browser history -> standardized JSON
app/                         # ingest, normalize, storage, stats, topics,
                             # embeddings, umap_space, pageviews, api
scripts/                     # build_reference_space.py, check_env.py
data/                        # local DB + reference artifacts (git-ignored)
tests/                       # pytest suite
reference_code/              # prior exploratory scripts (read-only reference)
```
