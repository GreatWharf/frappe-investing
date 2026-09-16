"""Shared market-data provider plumbing.

Providers return plain quote dicts ``{security_key, day, close, currency}``
and search-result dicts. HTTP safety (trust_env=False sessions, 30s
timeout, TLS verify, 20 MB response cap, sanitized errors, 429 handling)
is shared with the broker connectors from ``connectors.base``.
"""

from ..connectors.base import http_json, http_request, new_session

__all__ = ["MarketDataError", "ProviderBase", "http_json", "http_request", "new_session"]


class MarketDataError(Exception):
    """Provider failure. ``code`` is machine-stable; the message is sanitized."""

    def __init__(self, message, code="marketdata_error"):
        super().__init__(message)
        self.code = code


class ProviderBase:
    """Base class for market-data providers. Session is injectable for offline tests."""

    name = "base"
    supported = frozenset()  # subset of {"daily_prices", "search"}

    def __init__(self, session=None):
        self.session = session or new_session()

    def capabilities(self):
        return {"daily_prices": "daily_prices" in self.supported, "search": "search" in self.supported}

    def daily_prices(self, security_keys, day):
        """Quotes for the given keys on one day. Missing data is skipped, never fabricated."""
        raise NotImplementedError

    def search(self, query):
        """Security search -> list of provider-specific dicts."""
        raise NotImplementedError


def day_str(day):
    """Accept a date or an ISO string; normalize to YYYY-MM-DD."""
    return day.isoformat() if hasattr(day, "isoformat") else str(day)
