"""One-time/refreshable build of the frozen UMAP reference space (repo_spec
§7.5, D4).

Samples ~40k articles (30k top-viewed head from a pageviews dump + 10k
random tail), labels topics, fetches summaries, embeds, fits UMAP once, and
persists reducer + reference embeddings + coords + metadata to
data/reference/. Resumable via the DB caches; --limit for fast test builds.

Implemented in Step 8.
"""
