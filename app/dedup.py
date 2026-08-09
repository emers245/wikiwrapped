"""Collapse the same physical visit seen by more than one source (D11).

The problem: one visit can arrive twice, once from the local Chrome SQLite
read and once from a Takeout export. Both describe the same event, but they
come from different export mechanisms, so exact timestamp equality is not
guaranteed a priori. Quiet duplicates would inflate every downstream
statistic, and that is a bug which looks fine at a thousand rows.

What is measured so far (2026-08-09, scripts/compare_sources.py, against a
REAL Takeout archive):

- Chrome's local visits.visit_time is microseconds since 1601. Sampled over
  826 local Wikipedia visit rows, 0 had a zero-microsecond residue, so the
  local path carries real sub-second precision and is not rounding.
- Takeout's time_usec is microseconds since 1970, as a string. It is NOT the
  1601 epoch, whatever repo_spec S5 and the old reference scripts say; see
  extract_history.usec_to_unix for what that assumption cost.
- Of the visits appearing in both a real Takeout export and the local Chrome
  DB over their overlapping window, the timestamp delta was 0 seconds.

So exact same-second matching is the measured default, not a guess.

That comparison had n=1, and it will stay small. The reason also happens to
be why it matters less than it looks:

- Takeout is authorized for the personal account. The busy local profile
  belongs to a Workspace account whose administrator has Takeout disabled,
  so it can never be a Takeout source. That is settled, not pending.
- The two inputs therefore cover almost disjoint accounts. Cross-source
  duplicates can only occur where they overlap, which today is a single
  visit, and grows only as the personal profile is used locally.

So the exposure to a wrong tolerance is small by construction. The two
places dedup actually does load-bearing work are both same-mechanism, where
timestamps are byte-identical and exact matching is provably right:

- consecutive Takeout archives, which re-export months of the same visits
- re-reading the same local Chrome profile on every sync

Raise DEDUP_TOLERANCE_S only if compare_sources.py ever reports a non-zero
delta distribution over a real sample. Nothing else has to change, because
collapse() already clusters with tolerance.
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable

# Seconds of slack when deciding whether two records are the same visit.
# 0 means "identical truncated second", which is what both current paths
# already produce. Raise only on evidence from compare_sources.py; every
# increase risks merging two genuine rapid revisits into one.
DEDUP_TOLERANCE_S = 0

# Above this, a "tolerance" would start eating real reading behavior: a
# revisit within a couple of minutes is common and genuinely two visits.
MAX_SANE_TOLERANCE_S = 60


def parse_iso(timestamp: str) -> int:
    """UTC ISO-8601 -> integer Unix seconds (the extractor emits whole
    seconds, so nothing is lost by truncating)."""
    text = timestamp.strip().replace("Z", "+00:00")
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


def format_iso(unix_s: int) -> str:
    return dt.datetime.fromtimestamp(unix_s, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def collapse(
    entries: Iterable[dict],
    tolerance_s: int | None = None,
) -> list[dict]:
    """Group records describing one physical visit into a single visit.

    Each entry needs:
      key       - the identity the visit is deduped on. Callers pass the
                  CANONICAL post-redirect title, not the raw URL, so that
                  /wiki/Foo, /wiki/foo?action=raw and a redirect source and
                  its target all collapse to one page (repo_spec D5).
      timestamp - UTC ISO-8601.
      source    - "chrome_local" | "takeout".
      account   - Google account, or None.

    Returns one dict per collapsed visit:
      {key, timestamp, sources: [{source, account}, ...], record_count}
    where timestamp is the EARLIEST in the cluster. With tolerance 0 that is
    just "the" timestamp; with tolerance above 0 it keeps the result stable
    regardless of the order records arrive in.

    Clustering compares each timestamp against the START of the open cluster,
    not its previous member, so a dense run of visits cannot chain into one
    arbitrarily wide cluster.
    """
    tolerance = DEDUP_TOLERANCE_S if tolerance_s is None else tolerance_s
    if tolerance < 0:
        raise ValueError("Dedup tolerance cannot be negative.")
    if tolerance > MAX_SANE_TOLERANCE_S:
        raise ValueError(
            f"Dedup tolerance of {tolerance}s exceeds the {MAX_SANE_TOLERANCE_S}s "
            "sanity cap; a window this wide merges genuine revisits. Raise "
            "MAX_SANE_TOLERANCE_S deliberately if the measurement really "
            "justifies it."
        )

    by_key: dict[str, list[tuple[int, dict]]] = {}
    for entry in entries:
        key = entry.get("key")
        stamp = entry.get("timestamp")
        if not key or not stamp:
            continue
        by_key.setdefault(key, []).append((parse_iso(stamp), entry))

    collapsed: list[dict] = []
    for key in sorted(by_key):
        rows = sorted(by_key[key], key=lambda pair: pair[0])
        cluster_start: int | None = None
        cluster: list[dict] = []
        for unix_s, entry in rows:
            if cluster_start is None or unix_s - cluster_start > tolerance:
                if cluster:
                    collapsed.append(_finish(key, cluster_start, cluster))
                cluster_start, cluster = unix_s, [entry]
            else:
                cluster.append(entry)
        if cluster:
            collapsed.append(_finish(key, cluster_start, cluster))

    collapsed.sort(key=lambda visit: (visit["timestamp"], visit["key"]))
    return collapsed


def _finish(key: str, cluster_start: int, cluster: list[dict]) -> dict:
    """Turn one cluster into a visit with merged, deduped provenance."""
    seen: set[tuple] = set()
    sources: list[dict] = []
    for entry in cluster:
        pair = (entry.get("source"), entry.get("account"))
        if pair[0] and pair not in seen:
            seen.add(pair)
            sources.append({"source": pair[0], "account": pair[1]})
    sources.sort(key=lambda item: (item["source"], item["account"] or ""))
    return {
        "key": key,
        "timestamp": format_iso(cluster_start),
        "sources": sources,
        "record_count": len(cluster),
    }
