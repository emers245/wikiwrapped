"""Shared offline test doubles for the Wikimedia HTTP layer."""

import requests


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns queued responses and records each request's params."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self._responses.pop(0)


def api_payload(normalized=(), redirects=(), pages=(), missing=(), invalid=()):
    """Build a formatversion=2 MediaWiki action=query response."""
    return {
        "query": {
            "normalized": [{"from": f, "to": t} for f, t in normalized],
            "redirects": [{"from": f, "to": t} for f, t in redirects],
            "pages": (
                [{"title": t} for t in pages]
                + [{"title": t, "missing": True} for t in missing]
                + [{"title": t, "invalid": True} for t in invalid]
            ),
        }
    }
