"""URL -> canonical Wikipedia article title (repo_spec §7.1, D5).

The join key for topics, embeddings, and pageview stats; errors here silently
drop data downstream, so this module is built and tested first.

Contract:
- url_to_title(url) -> str | None: pure parsing, no network. Handles
  %-encoding, #fragments, ?query params, underscores vs spaces, mobile hosts,
  and returns None for non-article namespaces (File:, Special:, Talk:, ...).
- resolve_redirects(titles) -> dict: batched MediaWiki Action API lookups
  (action=query&redirects=1) mapping title -> canonical title.
- canonicalize(urls, ...) -> dict: composition used by ingest; cache-aware.

Implemented in Step 2.
"""
