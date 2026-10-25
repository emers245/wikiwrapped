"""Shared Wikimedia HTTP etiquette (repo_spec S8).

One place for the rules every API client must follow: identify the tool
with a way to reach its operator (User-Agent and Api-User-Agent; the
project URL, see config.get_user_agent), and back off exponentially on HTTP
429/5xx. Used by normalize, topics, embeddings, and pageviews so
the policy exists exactly once.

(Not in the S4 module list; factored out when a second client needed the
same session/retry code.)
"""

from __future__ import annotations

import logging
import time

import requests

import config

log = logging.getLogger(__name__)


def make_session() -> requests.Session:
    """Session carrying the Wikimedia-required identification headers."""
    session = requests.Session()
    user_agent = config.get_user_agent()
    session.headers.update(
        {"User-Agent": user_agent, "Api-User-Agent": user_agent}
    )
    return session


def request_with_retries(
    session: requests.Session, method: str, url: str, **kwargs
) -> requests.Response:
    """Issue one logical request, retrying 429/5xx with exponential backoff.

    Returns the response for any non-retryable status (2xx, 3xx, or 4xx
    other than 429) - the caller decides what a non-2xx means for it.
    Raises HTTPError only when retries are exhausted on a retryable status.
    """
    kwargs.setdefault("timeout", config.REQUEST_TIMEOUT_S)
    delay = config.BACKOFF_BASE_S
    for attempt in range(config.MAX_RETRIES + 1):
        response = getattr(session, method)(url, **kwargs)
        retryable = response.status_code == 429 or response.status_code >= 500
        if not retryable:
            return response
        if attempt == config.MAX_RETRIES:
            response.raise_for_status()
        log.info(
            "HTTP %d from %s; retrying in %.1fs", response.status_code, url, delay
        )
        time.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")
