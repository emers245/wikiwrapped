"""URL -> canonical Wikipedia article title (repo_spec S7.1, D5).

The canonical title is the join key for topics, embeddings, and pageview
stats; errors here silently drop data downstream, so this module is built
and tested first.

Three layers:
- url_to_title(url): pure parsing, no network. Extracts a clean title or
  returns None for anything that is not an article on the configured
  language's Wikipedia (non-article namespaces, other hosts, other langs).
- resolve_redirects(titles): batched MediaWiki Action API lookups mapping
  each title to its post-redirect canonical form (None if the page does
  not exist).
- canonicalize(urls, redirect_cache): the composition ingest uses; consults
  and fills a dict-like redirect cache so nothing is looked up twice (D6).

Scope note: only the config.WIKI_LANG Wikipedia (desktop and mobile hosts)
is accepted; visits to other-language wikis return None because every
downstream API call (topics, summaries, pageviews) is language-specific.
Ingest logs what was dropped so this is never silent.
"""

from __future__ import annotations

import time
from typing import Iterable, MutableMapping
from urllib.parse import parse_qs, unquote, urlparse

import requests

import config
from app import wiki_http

# MediaWiki Action API caps titles= at 50 per request for anonymous clients.
BATCH_SIZE = 50

# Standard + talk namespaces on en.wikipedia, lowercased, underscores as
# spaces. A colon-prefixed title is only rejected if its prefix is here, so
# articles like "Star Trek: Voyager" survive.
NON_ARTICLE_NAMESPACES = frozenset({
    "book", "book talk",
    "category", "category talk",
    "draft", "draft talk",
    "education program", "education program talk",
    "file", "file talk",
    "gadget", "gadget talk",
    "gadget definition", "gadget definition talk",
    "help", "help talk",
    "image", "image talk",
    "media",
    "mediawiki", "mediawiki talk",
    "module", "module talk",
    "portal", "portal talk",
    "special",
    "talk",
    "template", "template talk",
    "timedtext", "timedtext talk",
    "user", "user talk",
    "wikipedia", "wikipedia talk",
    "wp", "wt",
})


def url_to_title(url: str) -> str | None:
    """Extract a clean article title from a Wikipedia URL, no network.

    Returns None for non-article pages (namespaces like File:/Special:),
    non-Wikipedia hosts, other-language editions, and unparseable URLs.
    The result uses spaces (not underscores) with the first letter
    uppercased; redirects are NOT resolved here (see resolve_redirects).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not _is_article_host(host):
        return None

    if parsed.path.startswith("/wiki/"):
        slug = parsed.path[len("/wiki/"):]
    elif parsed.path == "/w/index.php":
        # Old-style URLs: /w/index.php?title=Foo&action=view
        slug = _query_param(parsed.query, "title")
    else:
        return None
    if not slug:
        return None

    title = unquote(slug)
    # Fragments are split off by urlparse, but a %23 in the path decodes to
    # a literal "#", which cannot appear in a title.
    title = title.split("#", 1)[0]
    title = " ".join(title.replace("_", " ").split())
    if not title or _is_non_article_namespace(title):
        return None
    # MediaWiki titles are first-letter case-insensitive, stored uppercased.
    return title[0].upper() + title[1:]


def resolve_redirects(
    titles: Iterable[str],
    session: requests.Session | None = None,
) -> dict[str, str | None]:
    """Map each title to its canonical (post-redirect) title via the
    MediaWiki Action API, batched at BATCH_SIZE titles per request.

    Applies the API's title normalization (case, underscores) and follows
    redirect chains. Titles that do not exist or are invalid map to None.
    """
    session = session or wiki_http.make_session()
    todo = [t for t in dict.fromkeys(titles) if t]
    result: dict[str, str | None] = {}
    # "|" is the API list separator and illegal in titles; do not batch it.
    todo, bad = [t for t in todo if "|" not in t], [t for t in todo if "|" in t]
    result.update({t: None for t in bad})

    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        data = _api_query(session, batch)
        result.update(_map_batch(batch, data))
        if start + BATCH_SIZE < len(todo):
            time.sleep(config.THROTTLE_S)
    return result


def canonicalize(
    urls: Iterable[str],
    redirect_cache: MutableMapping[str, str | None] | None = None,
    session: requests.Session | None = None,
) -> dict[str, str | None]:
    """URL -> canonical title for a batch of URLs; the ingest entry point.

    redirect_cache maps raw title -> canonical title (or None); it is
    consulted before the network and updated with anything newly resolved,
    so callers can persist it between runs (D6).
    """
    cache = redirect_cache if redirect_cache is not None else {}
    url_titles = {url: url_to_title(url) for url in dict.fromkeys(urls)}
    unresolved = sorted(
        {t for t in url_titles.values() if t is not None and t not in cache}
    )
    if unresolved:
        cache.update(resolve_redirects(unresolved, session=session))
    return {
        url: (cache[t] if t is not None else None)
        for url, t in url_titles.items()
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _is_article_host(host: str) -> bool:
    """True for the configured language's desktop or mobile host."""
    parts = host.split(".")
    if parts[-2:] != ["wikipedia", "org"] or parts[0] != config.WIKI_LANG:
        return False
    return len(parts) == 3 or (len(parts) == 4 and parts[1] == "m")


def _is_non_article_namespace(title: str) -> bool:
    if ":" not in title:
        return False
    prefix = title.split(":", 1)[0].strip().lower()
    return prefix in NON_ARTICLE_NAMESPACES


def _query_param(query: str, key: str) -> str | None:
    values = parse_qs(query).get(key)
    return values[0] if values else None


def _api_query(session: requests.Session, batch: list[str]) -> dict:
    """One Action API request (retry/backoff handled by wiki_http)."""
    response = wiki_http.request_with_retries(
        session,
        "get",
        config.MEDIAWIKI_API_URL,
        params={
            "action": "query",
            "redirects": 1,
            "titles": "|".join(batch),
            "format": "json",
            "formatversion": 2,
        },
    )
    response.raise_for_status()
    return response.json()


def _map_batch(batch: list[str], data: dict) -> dict[str, str | None]:
    """Map each requested title through the API's normalized + redirects
    mappings to an existing page title, or None."""
    query = data.get("query", {})
    normalized = {e["from"]: e["to"] for e in query.get("normalized", [])}
    redirects = {e["from"]: e["to"] for e in query.get("redirects", [])}
    existing = {
        page["title"]
        for page in query.get("pages", [])
        if page.get("title")
        and not page.get("missing")
        and not page.get("invalid")
    }

    out: dict[str, str | None] = {}
    for original in batch:
        title = normalized.get(original, original)
        seen = {title}
        while title in redirects:  # chains arrive as individual hops
            title = redirects[title]
            if title in seen:  # self-referential loop: treat as dead end
                break
            seen.add(title)
        out[original] = title if title in existing else None
    return out
