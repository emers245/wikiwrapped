"""Article summary fetch + sentence-transformer encoding (repo_spec §7.4).

Fetches description + extract from the Wikipedia REST summary endpoint,
encodes with all-MiniLM-L6-v2 (normalized vectors), caches in
embedding_cache keyed by (title, model_name) (D6).

Implemented in Step 7.
"""
