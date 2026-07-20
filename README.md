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
`/stats/busiest-week?tz=`, `/stats/topics`, `/map`, `/pages/{title}`.
Interactive docs at `localhost:8000/docs`. Run tests with
`pytest` (`-m "not integration"` to stay offline).

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
