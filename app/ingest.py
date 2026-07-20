"""Load standardized history JSON into the DB (repo_spec §5, D1, D2).

Validates schema_version, normalizes URLs to canonical titles (via
app.normalize, cache-aware), upserts pages, inserts visits. Idempotent:
re-ingesting the same or an updated export never duplicates visits.

Implemented in Step 4.
"""
