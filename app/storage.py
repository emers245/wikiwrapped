"""SQLite schema + thin data-access layer (repo_spec §6, D6).

Tables: visits, pages, topic_cache, pageview_cache, embedding_cache,
umap_coords. All SQL lives here; other modules call typed helpers.
SQLite chosen over DuckDB (decision 2026-07-19): stdlib, zero added
dependency risk, sufficient for this data scale; layer kept thin so a
swap stays cheap.

Implemented in Step 4.
"""
