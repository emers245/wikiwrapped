#!/usr/bin/env python3
"""Measure how Takeout and local-Chrome timestamps differ (D11 calibration).

This answers the one question app/dedup.py cannot answer on its own: when the
same physical visit arrives through two different export mechanisms, are the
timestamps byte-identical, or do they drift? If they drift, exact matching
silently fails to dedupe and every downstream statistic is inflated by a
margin that looks fine at a thousand rows.

    python scripts/compare_sources.py --takeout takeout-20260809T120000Z-001.zip
    python scripts/compare_sources.py --takeout export.json --local other.json
    python scripts/compare_sources.py --takeout a.zip --by-title

Both inputs may be a Takeout .zip, a JSON export, or a Chrome History DB;
--local defaults to reading this machine's Chrome profiles. Reports:

  - visits matching exactly on (url, timestamp)
  - visits matching on url within 1s / 60s / same calendar day
  - the distribution of timestamp deltas across url matches

Then set DEDUP_TOLERANCE_S in app/dedup.py from the delta distribution.
A tolerance is only justified by deltas that are consistently non-zero;
if the deltas are all zero, keep it at 0, because every second of slack
risks merging two genuine rapid revisits into one.

Deliberately a standalone analysis script, not part of the app: it exists to
inform one constant, and it reads nothing the pipeline depends on.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extract import extract_history as eh   # noqa: E402


def load_any(path: Path) -> list[dict]:
    """Load visits from a Takeout zip, a JSON export, or a Chrome History DB."""
    with open(path, "rb") as handle:
        magic = handle.read(16)
    if magic.startswith(eh.SQLITE_MAGIC):
        rows, kind = eh.load_chrome_db(path), "Chrome DB"
    elif magic.startswith(eh.ZIP_MAGIC):
        rows, kind = eh.load_takeout_zip(path), "Takeout zip"
    else:
        rows, kind = eh.load_json_export(path), "JSON export"
    visits, counts = eh.build_visits(rows)
    print(f"  {path.name}: {kind}, {counts['read']} rows -> "
          f"{counts['kept']} Wikipedia visits")
    return visits


def load_local_profiles() -> list[dict]:
    """Every local Chrome profile, merged: the sync pass reads all of them."""
    merged: list[dict] = []
    for profile in eh.find_chrome_profiles():
        rows = eh.load_chrome_db(Path(profile["path"]))
        visits, counts = eh.build_visits(rows)
        print(f"  Chrome profile {profile['display_name']!r} "
              f"[{profile['directory']}]: {counts['kept']} Wikipedia visits")
        merged.extend(visits)
    seen, unique = set(), []
    for visit in merged:
        key = (visit["url"], visit["timestamp"])
        if key not in seen:
            seen.add(key)
            unique.append(visit)
    return unique


def to_unix(stamp: str) -> int:
    return int(
        dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    )


def span(visits: list[dict]) -> tuple[str, str] | None:
    if not visits:
        return None
    stamps = [v["timestamp"] for v in visits]
    return min(stamps), max(stamps)


def overlap_window(a: list[dict], b: list[dict]) -> tuple[str, str] | None:
    """The period both sources cover; outside it, absence proves nothing."""
    sa, sb = span(a), span(b)
    if not sa or not sb:
        return None
    start, end = max(sa[0], sb[0]), min(sa[1], sb[1])
    return (start, end) if start <= end else None


def normalize_url(url: str, by_title: bool) -> str:
    """Match key. By default the raw URL; --by-title uses the parsed article
    title, which is closer to what ingest actually dedupes on (the canonical
    post-redirect title, which needs the network and so is out of scope here).
    """
    if not by_title:
        return url
    from app.normalize import url_to_title

    return url_to_title(url) or url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Takeout and local Chrome timestamps for the "
                    "same visits, to calibrate the dedup tolerance."
    )
    parser.add_argument("--takeout", type=Path, required=True,
                        help="Takeout .zip or JSON export")
    parser.add_argument("--local", type=Path, default=None,
                        help="Chrome History DB or export "
                             "(default: this machine's Chrome profiles)")
    parser.add_argument("--by-title", action="store_true",
                        help="Match on parsed article title instead of raw URL")
    args = parser.parse_args(argv)

    print("Loading sources")
    takeout = load_any(args.takeout)
    local = load_any(args.local) if args.local else load_local_profiles()

    print(f"\nTakeout span: {span(takeout)}")
    print(f"Local span:   {span(local)}")

    window = overlap_window(takeout, local)
    if window is None:
        print(
            "\nNO OVERLAPPING PERIOD. The two sources cover disjoint date\n"
            "ranges, so no visit appears in both and nothing can be measured.\n"
            "Chrome keeps only ~90 days locally: request a Takeout export\n"
            "now, while it still overlaps the local window."
        )
        return 1
    print(f"Overlap:      {window[0]} to {window[1]}")

    # Only the overlap can be compared; outside it a missing visit means
    # "not exported", not "not deduped".
    t_in = [v for v in takeout if window[0] <= v["timestamp"] <= window[1]]
    l_in = [v for v in local if window[0] <= v["timestamp"] <= window[1]]
    print(f"\nIn overlap:   {len(t_in)} Takeout, {len(l_in)} local")
    if not t_in or not l_in:
        print("One source has no visits inside the overlap; nothing to compare.")
        return 1

    # 1. Exact (url, timestamp) equality: what dedup relies on today.
    t_exact = {(normalize_url(v["url"], args.by_title), v["timestamp"]) for v in t_in}
    l_exact = {(normalize_url(v["url"], args.by_title), v["timestamp"]) for v in l_in}
    exact = t_exact & l_exact
    print("\n" + "=" * 62)
    print("1. EXACT MATCH on (url, timestamp)")
    print("=" * 62)
    print(f"  matched:            {len(exact)}")
    print(f"  Takeout-only:       {len(t_exact - l_exact)}")
    print(f"  local-only:         {len(l_exact - t_exact)}")
    if t_exact:
        print(f"  matched / Takeout:  {len(exact) / len(t_exact):.1%}")

    # 2. URL matches within widening windows.
    by_url: dict[str, list[int]] = collections.defaultdict(list)
    for visit in l_in:
        by_url[normalize_url(visit["url"], args.by_title)].append(
            to_unix(visit["timestamp"])
        )
    for stamps in by_url.values():
        stamps.sort()

    deltas: list[int] = []
    unmatched_url = 0
    for visit in t_in:
        key = normalize_url(visit["url"], args.by_title)
        candidates = by_url.get(key)
        if not candidates:
            unmatched_url += 1
            continue
        target = to_unix(visit["timestamp"])
        deltas.append(min(abs(target - other) for other in candidates))

    print("\n" + "=" * 62)
    print("2. URL MATCH within a time window (nearest local visit)")
    print("=" * 62)
    print(f"  Takeout visits whose URL never appears locally: {unmatched_url}")
    for label, limit in (("same second", 0), ("1s", 1), ("60s", 60),
                         ("same day", 86400)):
        hits = sum(1 for d in deltas if d <= limit)
        share = f"{hits / len(deltas):.1%}" if deltas else "n/a"
        print(f"  within {label:12s} {hits:6d}  ({share})")

    # 3. The delta distribution: the number that sets the tolerance.
    print("\n" + "=" * 62)
    print("3. TIMESTAMP DELTA DISTRIBUTION for URL matches")
    print("=" * 62)
    if not deltas:
        print("  No URL matched between the two sources.")
        return 1
    ordered = sorted(deltas)
    def pct(p): return ordered[min(len(ordered) - 1, int(len(ordered) * p))]
    print(f"  n={len(ordered)}  min={ordered[0]}s  max={ordered[-1]}s")
    print(f"  median={pct(0.50)}s  p90={pct(0.90)}s  p99={pct(0.99)}s")
    print("\n  delta (s)   count")
    for value, count in sorted(collections.Counter(ordered).items())[:15]:
        print(f"  {value:9d}   {count}")

    zero = sum(1 for d in ordered if d == 0)
    print("\n" + "=" * 62)
    print("VERDICT")
    print("=" * 62)
    if zero == len(ordered):
        print("  Every matched visit agrees to the second.")
        print("  Keep DEDUP_TOLERANCE_S = 0.")
    else:
        print(f"  {len(ordered) - zero} of {len(ordered)} matches disagree "
              f"({(len(ordered) - zero) / len(ordered):.1%}).")
        print(f"  A tolerance of {pct(0.99)}s would cover 99% of them.")
        print("  Weigh that against merging genuine rapid revisits before")
        print("  raising DEDUP_TOLERANCE_S in app/dedup.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
