"""Standalone extractor: browser history -> standardized JSON (repo_spec S5, D1).

The only code that ever touches browser-specific formats. Reads either a
Chrome History SQLite DB (auto-located per-OS, copied to a temp file first
because Chrome locks it; per-visit rows from the visits table) or a
Takeout/extension JSON export (time_usec us-since-1601 or visitTime /
lastVisitTime ms-since-1970). Filters to Wikipedia hosts, normalizes all
timestamps to UTC ISO-8601, emits the schema_version 1.0 JSON.

Stdlib-only so it runs without the conda env. Implemented in Step 3.
"""
