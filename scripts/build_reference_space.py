"""One-time/refreshable build of the frozen UMAP reference space (repo_spec
S7.5, D4).

Pipeline (ported from the old head+tail sampler, ORES swapped for Lift
Wing):

1. Head: the N most-viewed en.wikipedia titles from a pageviews dump
   hour-file (namespace pages excluded).
2. Tail: M random mainspace titles via the MediaWiki random API.
3. Resolve redirects (batched, redirect_cache) so sample titles are
   canonical; dedupe.
4. Topic-label everything via Lift Wing (shared topic_cache).
5. Fetch summaries + embed via the shared embedding_cache - interrupted
   runs resume from cache.
6. Fit UMAP once (params from app.umap_space) and persist reducer +
   embeddings + labeled reference coords + build metadata.

At full scale (30k + 10k, the defaults) steps 4-5 are API-bound and take
HOURS; run overnight. Run scripts/fetch_model.py first, so the embeddings
come from the bundled model at the pinned revision (the build records
whether it did). Small builds for pipeline testing:

    python scripts/build_reference_space.py --head 1500 --tail 500 \
        --db /tmp/test.db --out /tmp/reference

The build's caches go in their own database (data/reference-build.db by
default), never the app's.

The default dump is the hour-file already in reference_code/; pass --dump
to use a fresher one from dumps.wikimedia.org/other/pageviews/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import pickle
import sys
import time
from pathlib import Path
from urllib.parse import unquote

# Standalone script: make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import config
from app import bundle, embeddings, normalize, storage, topics, umap_space, wiki_http

DEFAULT_DUMP = config.PROJECT_ROOT / "reference_code" / "pageviews-202505.gz"

# The build's own cache database (topics, embeddings, redirects for ~40k
# articles), kept apart from the app's database in the user-data dir so a
# build never bloats the user's own history database. Under the repo's
# data/, which is git-ignored and where this developer tool may write.
DEFAULT_BUILD_DB = config.PROJECT_ROOT / "data" / "reference-build.db"


def parse_dump(path: Path, n: int) -> list[str]:
    """Top-n en.wikipedia article titles by views from a pageviews
    hour-file (lines: project title views bytes)."""
    counts: list[tuple[str, int]] = []
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split(" ")
            if len(parts) != 4 or parts[0] != "en":
                continue
            title = unquote(parts[1]).replace("_", " ").strip()
            if not normalize.is_article_title(title) or title == "Main Page":
                continue
            try:
                views = int(parts[2])
            except ValueError:
                continue
            counts.append((title, views))
    counts.sort(key=lambda item: -item[1])
    seen: set[str] = set()
    top: list[str] = []
    for title, _ in counts:
        if title in seen:
            continue
        seen.add(title)
        top.append(title)
        if len(top) == n:
            break
    return top


def sample_random(n: int, session, exclude: set[str]) -> list[str]:
    """M random mainspace titles (rnnamespace=0 excludes non-articles;
    the API's default rnfilterredir excludes redirects)."""
    titles: list[str] = []
    seen = set(exclude)
    while len(titles) < n:
        response = wiki_http.request_with_retries(
            session,
            "get",
            config.MEDIAWIKI_API_URL,
            params={
                "action": "query",
                "list": "random",
                "rnnamespace": 0,
                "rnlimit": min(500, n - len(titles)),
                "format": "json",
                "formatversion": 2,
            },
        )
        response.raise_for_status()
        for item in response.json().get("query", {}).get("random", []):
            title = item.get("title")
            if title and title not in seen:
                seen.add(title)
                titles.append(title)
                if len(titles) == n:
                    break
        time.sleep(config.THROTTLE_S)
    return titles


def canonicalize_sample(conn, titles: list[str], session) -> list[str]:
    """Redirect-resolve sample titles through the shared redirect_cache;
    drop unresolvable ones; dedupe preserving order."""
    cache = storage.get_redirect_cache(conn)
    missing = [t for t in dict.fromkeys(titles) if t not in cache]
    if missing:
        cache.update(normalize.resolve_redirects(missing, session))
        storage.update_redirect_cache(
            conn, {t: cache[t] for t in missing if t in cache}
        )
        conn.commit()
    out: list[str] = []
    seen: set[str] = set()
    for title in titles:
        canonical = cache.get(title)
        if canonical and canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sample articles, embed them, fit UMAP once, and "
        "persist the frozen reference space (D4)."
    )
    parser.add_argument("--head", type=int, default=config.REFERENCE_HEAD_SIZE,
                        help="top-viewed titles to include (default %(default)s)")
    parser.add_argument("--tail", type=int, default=config.REFERENCE_TAIL_SIZE,
                        help="random titles to include (default %(default)s)")
    parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP,
                        help="pageviews hour-file .gz (default %(default)s)")
    parser.add_argument("--db", type=Path, default=DEFAULT_BUILD_DB,
                        help="cache database for the build (default %(default)s; "
                             "resumable: re-running reuses its caches)")
    parser.add_argument("--out", type=Path, default=config.DEV_REFERENCE_DIR,
                        help="artifact output dir (default %(default)s)")
    args = parser.parse_args(argv)

    timings: dict[str, float] = {}
    conn = storage.connect(args.db)
    session = wiki_http.make_session()

    t0 = time.time()
    head_raw = parse_dump(args.dump, args.head)
    print(f"head: {len(head_raw)} titles from {args.dump.name}")
    tail_raw = sample_random(args.tail, session, exclude=set(head_raw))
    print(f"tail: {len(tail_raw)} random titles")
    timings["sample_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    head = canonicalize_sample(conn, head_raw, session)
    tail = [t for t in canonicalize_sample(conn, tail_raw, session)
            if t not in set(head)]
    titles = head + tail
    source = {**{t: "head" for t in head}, **{t: "tail" for t in tail}}
    print(f"canonical sample: {len(titles)} unique titles "
          f"({len(head)} head, {len(tail)} tail)")
    timings["canonicalize_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    topic_report = topics.label_titles(conn, titles, session, progress=True)
    print(f"topics: {topic_report}")
    timings["topics_s"] = round(time.time() - t0, 1)

    t0 = time.time()
    embed_report = embeddings.embed_titles(conn, titles, session, progress=True)
    print(f"embeddings: {embed_report}")
    timings["embeddings_s"] = round(time.time() - t0, 1)

    vectors = storage.get_embeddings(conn, config.EMBEDDING_MODEL, titles)
    ordered = [t for t in titles if t in vectors]
    matrix = np.vstack([vectors[t] for t in ordered]).astype(np.float32)
    print(f"fitting UMAP on {matrix.shape[0]} x {matrix.shape[1]} embeddings")

    t0 = time.time()
    import umap  # deferred: heavy numba JIT on import

    reducer = umap.UMAP(**umap_space.UMAP_PARAMS, verbose=True).fit(matrix)
    coords = reducer.embedding_
    timings["umap_fit_s"] = round(time.time() - t0, 1)

    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / umap_space.REDUCER_FILE, "wb") as handle:
        pickle.dump(reducer, handle, protocol=4)
    np.save(args.out / umap_space.EMBEDDINGS_FILE, matrix)

    topic_cache = storage.get_topic_cache(conn)
    points = [
        {
            "title": title,
            "macro_topic": (topic_cache.get(title) or {}).get("macro_topic"),
            "x": float(x),
            "y": float(y),
            "source": source[title],
        }
        for title, (x, y) in zip(ordered, coords)
    ]
    (args.out / umap_space.POINTS_FILE).write_text(
        json.dumps(points), encoding="utf-8"
    )

    import sentence_transformers

    meta = {
        "built_at": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "embedding_model": config.EMBEDDING_MODEL,
        # The exact weights the embeddings came from, and every library
        # version the pickled reducer depends on. Packaging refuses to ship
        # this space with a runtime whose versions differ (app/bundle.py).
        "embedding_model_revision": config.EMBEDDING_MODEL_REVISION,
        "embedding_model_bundled": (config.EMBEDDING_MODEL_DIR / "modules.json").is_file(),
        "versions": bundle.runtime_versions(),
        "umap_params": umap_space.UMAP_PARAMS,
        "umap_version": umap.__version__,
        "sentence_transformers_version": sentence_transformers.__version__,
        "head_requested": args.head,
        "tail_requested": args.tail,
        "titles_fitted": len(ordered),
        "dump_file": args.dump.name,
        "timings": timings,
    }
    (args.out / umap_space.META_FILE).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    conn.close()
    print(f"wrote reference space ({len(ordered)} points) to {args.out}")
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
