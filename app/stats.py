"""Basic reading stats returning JSON-ready data structures, never plots
(repo_spec S7.3, D2).

D2 discipline: frequency/time stats (grid, streaks, busiest week) are
computed over visits; "pages visited" counts unique pages.

Every time-of-day function takes an optional tz argument (IANA zone name);
resolution order is argument > config.TIMEZONE_OVERRIDE > device zone (see
config.get_timezone). Conversions use proper IANA zones, so DST is handled
correctly, unlike the old fixed UTC-6 offset.

All SQL lives in app.storage; this module only aggregates in Python, which
is ample at personal-history scale.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections import Counter, deque

import config
from app import storage

DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


def summary(conn: sqlite3.Connection) -> dict:
    """Unique pages (D2), total visits, and the UTC date range covered."""
    first, last = storage.visit_date_range(conn)
    return {
        "unique_pages": storage.count_pages(conn),
        "total_visits": storage.count_visits(conn),
        "first_visit": first,
        "last_visit": last,
    }


def most_visited(conn: sqlite3.Connection, n: int = 10) -> list[dict]:
    """Top pages by visit count (ties broken alphabetically for stability)."""
    return storage.top_pages(conn, n)


def weekday_hour_grid(conn: sqlite3.Connection, tz: str | None = None) -> dict:
    """7x24 visit counts by local weekday and hour.

    Raw counts, not densities: normalization is a presentation decision
    (the old code normalized per-day; the frontend can do either).
    """
    zone, local_times = _local_visit_times(conn, tz)
    counts = [[0] * 24 for _ in DAY_NAMES]
    for moment in local_times:
        counts[moment.weekday()][moment.hour] += 1
    return {
        "timezone": str(zone),
        "days": DAY_NAMES,
        "hours": list(range(24)),
        "counts": counts,
    }


def daily_series(
    conn: sqlite3.Connection,
    tz: str | None = None,
    rolling_window: int = 7,
) -> dict:
    """Visits per local calendar day, zero-filled over the whole range,
    with a trailing rolling average (None until the window is full,
    matching the old pandas rolling(7).mean() behavior)."""
    zone, local_times = _local_visit_times(conn, tz)
    result = {"timezone": str(zone), "rolling_window": rolling_window, "days": []}
    if not local_times:
        return result

    per_day = Counter(moment.date() for moment in local_times)
    window: deque[int] = deque(maxlen=rolling_window)
    day = min(per_day)
    last = max(per_day)
    while day <= last:
        visits = per_day.get(day, 0)
        window.append(visits)
        rolling = (
            round(sum(window) / rolling_window, 3)
            if len(window) == rolling_window
            else None
        )
        result["days"].append(
            {"date": day.isoformat(), "visits": visits, "rolling_avg": rolling}
        )
        day += dt.timedelta(days=1)
    return result


def busiest_week(conn: sqlite3.Connection, tz: str | None = None) -> dict | None:
    """The ISO calendar week (local time) with the most visits; earliest
    week wins ties. None when there are no visits."""
    zone, local_times = _local_visit_times(conn, tz)
    if not local_times:
        return None
    per_week = Counter(
        (moment.isocalendar().year, moment.isocalendar().week)
        for moment in local_times
    )
    (iso_year, iso_week), visits = sorted(
        per_week.items(), key=lambda item: (-item[1], item[0])
    )[0]
    monday = dt.date.fromisocalendar(iso_year, iso_week, 1)
    return {
        "timezone": str(zone),
        "iso_year": iso_year,
        "iso_week": iso_week,
        "start_date": monday.isoformat(),
        "end_date": (monday + dt.timedelta(days=6)).isoformat(),
        "visits": visits,
    }


def topic_distribution(conn: sqlite3.Connection) -> dict:
    """Fraction of unique pages (D2) per macro-topic and per raw label.

    Pages without a cached topic report as "Unclassified" (matching the
    old analysis) rather than being dropped.

    That bucket conflates two very different things, so `pages_unlabeled`
    reports the second of them separately: a page the topic model declined
    to classify has a cache row with NULL labels, while a page that has
    simply never been sent to the model has no cache row at all. The first
    is a fact about the page; the second means /enrich has not caught up
    with the last ingest, and looks identical in the chart. It is the
    normal state after a bulk import, and without this split the only
    symptom is one very large grey bar.
    """
    rows = storage.page_topics(conn)
    total = len(rows)
    macro = Counter(row["macro_topic"] or "Unclassified" for row in rows)
    raw = Counter(row["raw_label"] or "Unclassified" for row in rows)
    unlabeled = sum(1 for row in rows if not row["has_topic_row"])

    def as_list(counter: Counter, key: str) -> list[dict]:
        return [
            {key: label, "pages": pages,
             "fraction": round(pages / total, 4) if total else 0.0}
            for label, pages in sorted(
                counter.items(), key=lambda item: (-item[1], item[0])
            )
        ]

    return {
        "total_pages": total,
        "pages_unlabeled": unlabeled,
        "macro_topics": as_list(macro, "macro_topic"),
        "raw_labels": as_list(raw, "raw_label"),
    }


def _local_visit_times(
    conn: sqlite3.Connection, tz: str | None
) -> tuple[dt.tzinfo, list[dt.datetime]]:
    """All visit timestamps converted from stored UTC to the target zone."""
    zone = config.get_timezone(tz)
    return zone, [
        dt.datetime.fromisoformat(stamp).astimezone(zone)
        for stamp in storage.visit_timestamps(conn)
    ]
