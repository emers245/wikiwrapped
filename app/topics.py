"""Lift Wing outlink-topic-model client + macro() collapsing (repo_spec §7.2, D3).

POSTs canonical titles to the Lift Wing inference API (ORES is deprecated;
no revision-ID lookup needed). macro() keeps the first-level topic except
Culture.*, which keeps its second level. Results cached in topic_cache (D6);
unclassifiable titles are cached too so they aren't re-fetched.

Implemented in Step 6.
"""
