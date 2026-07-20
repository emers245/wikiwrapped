# wikipedia-wrapped

A visualization tool for personalized Wikipedia reading history: export your
browser history, extract the Wikipedia visits, and explore stats, topics, and
an embedding map of what you read.

Specification: `repo_spec.md` · Phase 1 plan: `claude_phase1.md`

## Setup

Building the conda environment is step zero (repo_spec §9) — verify it before
anything else:

```bash
conda env create -f environment.yml
conda activate wikipedia-wrapped
python scripts/check_env.py --full
```

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
