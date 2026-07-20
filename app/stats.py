"""Basic reading stats returning JSON-ready data structures, never plots
(repo_spec S7.3, D2).

Frequency/time stats are computed over visits; "pages visited" counts unique
pages. Time-of-day stats are timezone-aware: IANA zone from config
(device-detected by default), overridable per call.

Functions: summary, most_visited, weekday_hour_grid, daily_series,
busiest_week, topic_distribution.

Implemented in Step 5.
"""
