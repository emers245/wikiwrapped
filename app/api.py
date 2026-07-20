"""FastAPI app exposing computed results as JSON (repo_spec S7.7).

Thin by design - all logic lives in the sibling modules; this is the seam
the Phase 2 frontend consumes. Endpoints: /health, /ingest, /enrich,
/stats/*, /map, /pages/{title}.

Implemented in Step 10.
"""
