"""Per-article Pageviews REST API client (repo_spec S7.6, D7).

D7: popularity is measured over the SAME date range the user's history
covers (derived from the visits table), never all-time. Cache is keyed by
(title, date_start, date_end) (D6); a range change naturally misses the
cache and refetches. The API 404s when a title has no data in the range -
cached as NULL so it is not re-asked, reported as no_data.

Phase 1 ships the client + cache only; the popularity-comparison
analytics built on top are Phase 2.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from urllib.parse import quote

from tqdm import tqdm

import config
from app import storage, wiki_http

log = logging.getLogger(__name__)


def history_date_range(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """The user's history span as YYYYMMDD bounds (D7); None if no visits.

    Always the WHOLE history, even when the dashboard is showing only some
    accounts: the pageview cache is keyed on this range, so a narrower one
    would find nothing cached and every page would read "n/a".
    """
    first, last = storage.full_visit_date_range(conn)
    if first is None:
        return None
    to_day = lambda stamp: dt.datetime.fromisoformat(stamp).strftime("%Y%m%d")  # noqa: E731
    return to_day(first), to_day(last)


def fetch_pageviews(
    title: str, date_start: str, date_end: str, session=None
) -> int | None:
    """Total user (non-bot) views for one article over the range, all
    access methods; None when the API has no data (404)."""
    session = session or wiki_http.make_session()
    url = (
        f"{config.PAGEVIEWS_API_URL}/"
        f"{quote(title.replace(' ', '_'), safe='')}/daily/"
        f"{date_start}/{date_end}"
    )
    response = wiki_http.request_with_retries(session, "get", url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return sum(item.get("views", 0) for item in response.json().get("items", []))


def get_pageviews(
    conn: sqlite3.Connection,
    title: str,
    date_start: str,
    date_end: str,
    session=None,
) -> dict:
    """Cache-aware single-title lookup."""
    cached = storage.get_pageview_cache(conn, date_start, date_end)
    if title in cached:
        total = cached[title]
    else:
        total = fetch_pageviews(title, date_start, date_end, session)
        storage.upsert_pageviews(conn, title, date_start, date_end, total)
        conn.commit()
    return {
        "title": title,
        "date_start": date_start,
        "date_end": date_end,
        "total_views": total or 0,
        "no_data": total is None,
    }


def fetch_all(conn: sqlite3.Connection, session=None, progress: bool = False) -> dict:
    """Fetch pageviews for every visited page over the history range (the
    /enrich hook). Commits per page; interrupted runs resume."""
    span = history_date_range(conn)
    if span is None:
        return {"pages_fetched": 0, "pages_no_data": 0,
                "pages_already_cached": 0, "date_range": None}
    date_start, date_end = span

    cached = storage.get_pageview_cache(conn, date_start, date_end)
    todo = [t for t in storage.all_page_titles(conn) if t not in cached]
    already = storage.count_pages(conn) - len(todo)

    report = {"pages_fetched": 0, "pages_no_data": 0,
              "pages_already_cached": already,
              "date_range": [date_start, date_end]}
    if not todo:
        return report

    session = session or wiki_http.make_session()
    iterator = tqdm(todo, desc="pageviews", unit="page") if progress else todo
    for index, title in enumerate(iterator):
        total = fetch_pageviews(title, date_start, date_end, session)
        storage.upsert_pageviews(conn, title, date_start, date_end, total)
        conn.commit()
        if total is None:
            report["pages_no_data"] += 1
            log.info("no pageview data: %s", title)
        else:
            report["pages_fetched"] += 1
        if index < len(todo) - 1:
            time.sleep(config.THROTTLE_S)
    return report
