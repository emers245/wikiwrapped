"""Per-article Pageviews REST API client (repo_spec S7.6, D7).

Queries daily views over the user's history date range (not all-time) and
caches totals by (title, date_start, date_end) (D6). Phase 1 ships the
client + cache only; popularity-comparison analytics are Phase 2.

Implemented in Step 9.
"""
