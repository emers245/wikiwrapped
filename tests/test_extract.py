"""Tests for extract/extract_history.py (repo_spec S5, D1).

Covers both timestamp conventions (Chrome time_usec microseconds since
1601, export visitTime/lastVisitTime milliseconds since 1970), the host
filter, both JSON export layouts, and an end-to-end run against a
synthetic Chrome History SQLite DB (per-visit rows, D2).
"""

import datetime as dt
import json
import sqlite3

import pytest

from extract import extract_history as eh


# ---------------------------------------------------------------------------
# Timestamp conversions
# ---------------------------------------------------------------------------

def test_chrome_usec_epoch_offset():
    # The Unix epoch expressed in Chrome's 1601-based microseconds
    assert eh.chrome_usec_to_unix(eh.CHROME_EPOCH_OFFSET_S * 1_000_000) == 0.0


def test_chrome_usec_known_instant():
    # 2025-06-29T05:43:30Z as Chrome microseconds
    unix_s = dt.datetime(2025, 6, 29, 5, 43, 30, tzinfo=dt.timezone.utc).timestamp()
    usec = (unix_s + eh.CHROME_EPOCH_OFFSET_S) * 1_000_000
    assert eh.to_iso_utc(eh.chrome_usec_to_unix(usec)) == "2025-06-29T05:43:30Z"


def test_epoch_ms_handles_floats():
    # Real extension exports carry values like 1751175810689.08
    assert eh.epoch_ms_to_unix(1751175810689.08) == pytest.approx(1751175810.68908)
    assert eh.to_iso_utc(eh.epoch_ms_to_unix(1751175810689.08)) == "2025-06-29T05:43:30Z"


def test_non_numeric_timestamps_are_none():
    assert eh.chrome_usec_to_unix("garbage") is None
    assert eh.epoch_ms_to_unix(None) is None


def test_to_iso_utc_rejects_implausible():
    assert eh.to_iso_utc(None) is None
    assert eh.to_iso_utc(0) is None                     # 1970: zeroed field
    assert eh.to_iso_utc(-eh.CHROME_EPOCH_OFFSET_S) is None  # 1601: zeroed field
    assert eh.to_iso_utc(5_000_000_000) is None         # year 2128


def test_json_row_prefers_per_visit_time():
    # Export-extension rows carry both; visitTime is the per-visit one
    row = {"visitTime": 1_000_000.0, "lastVisitTime": 2_000_000.0}
    assert eh.json_row_to_unix(row) == 1_000.0
    assert eh.json_row_to_unix({"lastVisitTime": 2_000_000.0}) == 2_000.0
    # Takeout rows use time_usec
    usec = (1_000 + eh.CHROME_EPOCH_OFFSET_S) * 1_000_000
    assert eh.json_row_to_unix({"time_usec": usec}) == pytest.approx(1_000.0)
    assert eh.json_row_to_unix({"id": "1"}) is None


# ---------------------------------------------------------------------------
# Host filter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://en.wikipedia.org/wiki/Douglas_Adams", True),
        ("https://en.m.wikipedia.org/wiki/Douglas_Adams", True),
        ("https://de.wikipedia.org/wiki/Douglas_Adams", True),  # lang-agnostic here
        ("https://wikipedia.org/", True),
        ("https://commons.wikimedia.org/wiki/File:X.jpg", False),
        ("https://chromewebstore.google.com/detail/x", False),
        ("https://notwikipedia.org/wiki/X", False),
        ("", False),
    ],
)
def test_is_wikipedia_url(url, expected):
    assert eh.is_wikipedia_url(url) is expected


# ---------------------------------------------------------------------------
# JSON export layouts
# ---------------------------------------------------------------------------

WIKI_MS = 1751175810689.08  # 2025-06-29T05:43:30Z


def test_json_bare_list_extension_export(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps([
        {"url": "https://en.wikipedia.org/wiki/African_penguin",
         "title": "African penguin - Wikipedia", "visitTime": WIKI_MS},
        {"url": "https://chromewebstore.google.com/detail/x",
         "title": "Chrome Web Store", "visitTime": WIKI_MS},
    ]))
    visits, counts = eh.build_visits(eh.load_json_export(path))
    assert counts == {"read": 2, "non_wikipedia": 1, "bad_timestamp": 0,
                      "duplicate": 0, "kept": 1}
    assert visits == [{
        "url": "https://en.wikipedia.org/wiki/African_penguin",
        "title": "African penguin - Wikipedia",
        "timestamp": "2025-06-29T05:43:30Z",
    }]


def test_json_takeout_wrapper_with_time_usec(tmp_path):
    unix_s = dt.datetime(2025, 5, 1, 14, 32, tzinfo=dt.timezone.utc).timestamp()
    path = tmp_path / "takeout.json"
    path.write_text(json.dumps({"Browser History": [
        {"url": "https://en.wikipedia.org/wiki/Douglas_Adams",
         "title": "Douglas Adams - Wikipedia",
         "time_usec": (unix_s + eh.CHROME_EPOCH_OFFSET_S) * 1_000_000},
    ]}))
    visits, counts = eh.build_visits(eh.load_json_export(path))
    assert counts["kept"] == 1
    assert visits[0]["timestamp"] == "2025-05-01T14:32:00Z"


def test_json_unrecognized_layout_exits(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"something": "else"}))
    with pytest.raises(SystemExit):
        eh.load_json_export(path)


def test_duplicate_url_timestamp_rows_collapse(tmp_path):
    path = tmp_path / "dupes.json"
    row = {"url": "https://en.wikipedia.org/wiki/Douglas_Adams",
           "title": "Douglas Adams - Wikipedia", "visitTime": WIKI_MS}
    path.write_text(json.dumps([row, dict(row)]))
    visits, counts = eh.build_visits(eh.load_json_export(path))
    assert counts["duplicate"] == 1
    assert len(visits) == 1


# ---------------------------------------------------------------------------
# Chrome DB mode, end to end through main()
# ---------------------------------------------------------------------------

def make_chrome_db(path, entries):
    """entries: list of (url_id, url, title, [unix_visit_seconds, ...])"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
        "last_visit_time INTEGER)"
    )
    conn.execute(
        "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, "
        "visit_time INTEGER)"
    )
    for url_id, url, title, visit_times in entries:
        chrome_times = [
            int((t + eh.CHROME_EPOCH_OFFSET_S) * 1_000_000) for t in visit_times
        ]
        conn.execute(
            "INSERT INTO urls VALUES (?, ?, ?, ?)",
            (url_id, url, title, max(chrome_times, default=0)),
        )
        for stamp in chrome_times:
            conn.execute(
                "INSERT INTO visits (url, visit_time) VALUES (?, ?)",
                (url_id, stamp),
            )
    conn.commit()
    conn.close()


def test_chrome_db_end_to_end(tmp_path):
    base = dt.datetime(2025, 6, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    db_path = tmp_path / "History"
    make_chrome_db(db_path, [
        # Two visits to the same article must yield two rows (D2)
        (1, "https://en.wikipedia.org/wiki/Douglas_Adams",
         "Douglas Adams - Wikipedia", [base + 60, base]),
        (2, "https://www.google.com/search?q=adams", "Search", [base]),
        (3, "https://en.m.wikipedia.org/wiki/Cat", "Cat - Wikipedia", [base + 30]),
    ])
    out_path = tmp_path / "out.json"
    assert eh.main(["--input", str(db_path), "--output", str(out_path)]) == 0

    document = json.loads(out_path.read_text())
    assert document["schema_version"] == "1.0"
    assert document["source_browser"] == "chrome"
    visits = document["visits"]
    # google row is excluded by the SQL prefilter/host check; 3 wiki visits
    assert [v["url"] for v in visits] == [
        "https://en.wikipedia.org/wiki/Douglas_Adams",
        "https://en.m.wikipedia.org/wiki/Cat",
        "https://en.wikipedia.org/wiki/Douglas_Adams",
    ]
    # sorted by timestamp, second precision, Z suffix
    stamps = [v["timestamp"] for v in visits]
    assert stamps == sorted(stamps)
    assert stamps[0] == "2025-06-01T12:00:00Z"


def test_main_json_mode_detected_by_magic(tmp_path):
    src = tmp_path / "export.json"
    src.write_text(json.dumps([
        {"url": "https://en.wikipedia.org/wiki/Cat", "title": "Cat",
         "visitTime": WIKI_MS},
    ]))
    out = tmp_path / "out.json"
    assert eh.main(["--input", str(src), "--output", str(out)]) == 0
    assert len(json.loads(out.read_text())["visits"]) == 1
