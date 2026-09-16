"""Offline HTTP fakes and fixture loading for connector/marketdata tests.

No test ever touches the network: sessions are always these fakes, injected
into connectors and providers that accept a ``session=`` argument.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent


def load_fixture(name):
    """Read a fixture file as text."""
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class FakeResponse:
    """Minimal stand-in for requests.Response (streaming-capable)."""

    def __init__(self, status_code=200, body=b"", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}

    @property
    def content(self):
        return self.body

    @property
    def text(self):
        return self.body.decode("utf-8")

    def json(self, **kwargs):
        return json.loads(self.text, **kwargs)

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        pass


class FakeSession:
    """Records requests and serves routed or queued FakeResponses.

    ``add`` registers a persistent route matched on method + URL substring
    (the longest matching substring wins). ``push`` queues responses
    consumed in order when no route matches (use for poll sequences).
    """

    def __init__(self):
        self.calls = []
        self._routes = []
        self._queue = []
        self._raise = None
        self.trust_env = True

    def add(self, method, url_part, response):
        self._routes.append((method.upper(), url_part, response))
        return self

    def push(self, response):
        self._queue.append(response)
        return self

    def raise_next(self, exc):
        self._raise = exc
        return self

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method.upper(), "url": url, "kwargs": kwargs})
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc
        matches = [(part, resp) for m, part, resp in self._routes if m == method.upper() and part in url]
        if matches:
            return max(matches, key=lambda item: len(item[0]))[1]
        if self._queue:
            return self._queue.pop(0)
        raise AssertionError(f"FakeSession: no route for {method} {url}")
