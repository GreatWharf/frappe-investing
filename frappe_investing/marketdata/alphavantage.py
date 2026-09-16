"""Alpha Vantage market data (user's own API key required).

Terms note: Alpha Vantage's default free license is personal /
non-commercial; use in an open-source redistribution context needs written
confirmation from Alpha Vantage. This adapter is therefore opt-in: the user
supplies their own key and accepts the terms. No key ships with the app.

* TIME_SERIES_DAILY with ``outputsize=compact`` (last ~100 trading days);
  one request per symbol and the free tier allows ~25 requests/day, so bulk
  refresh is expensive — prefer Stooq or broker quotes first.
* A JSON body with a "Note" or "Information" key (their rate-limit /
  frequency envelope) maps to MarketDataError(code="rate_limited"); the
  body text is never echoed.
* The feed reports no currency, so quotes carry ``currency=None`` and the
  caller applies the Security's currency.
* Symbol mapping: ``US:AAPL`` -> ``AAPL`` (exchange prefix stripped); use
  ``symbol_map`` for non-US listings (e.g. ``{"NSE:RELIANCE": "RELIANCE.BSE"}``).
"""

from ..connectors.base import http_json
from .base import MarketDataError, ProviderBase, day_str


class AlphaVantageProvider(ProviderBase):
    name = "alphavantage"
    supported = frozenset({"daily_prices", "search"})
    URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key, symbol_map=None, session=None):
        super().__init__(session=session)
        self.api_key = api_key
        self.symbol_map = dict(symbol_map or {})

    def _get(self, params):
        payload = http_json(self.session, "GET", self.URL, params={**params, "apikey": self.api_key})
        if isinstance(payload, dict):
            if "Note" in payload or "Information" in payload:
                raise MarketDataError("Alpha Vantage rate limit reached", code="rate_limited")
            if "Error Message" in payload:
                raise MarketDataError("Alpha Vantage rejected the request", code="bad_symbol")
        return payload

    def _symbol(self, security_key):
        if security_key in self.symbol_map:
            return self.symbol_map[security_key]
        return security_key.split(":", 1)[1] if ":" in security_key else security_key

    def daily_prices(self, security_keys, day):
        target_day = day_str(day)
        quotes = []
        for key in security_keys:
            payload = self._get(
                {"function": "TIME_SERIES_DAILY", "symbol": self._symbol(key), "outputsize": "compact"}
            )
            series = payload.get("Time Series (Daily)") if isinstance(payload, dict) else None
            if series is None:
                raise MarketDataError("Alpha Vantage returned no daily series", code="bad_response")
            row = series.get(target_day)
            if not row:
                continue  # not in the compact window or non-trading day: skipped
            quotes.append(
                {
                    "security_key": key,
                    "day": target_day,
                    "close": str(row.get("4. close", "")),
                    "currency": None,
                }
            )
        return quotes

    def search(self, query):
        payload = self._get({"function": "SYMBOL_SEARCH", "keywords": query})
        matches = payload.get("bestMatches", []) if isinstance(payload, dict) else []
        results = []
        for match in matches:
            symbol = match.get("1. symbol", "")
            results.append(
                {
                    "security_key": symbol if "." in symbol else f"US:{symbol}",
                    "name": match.get("2. name", ""),
                    "type": match.get("3. type", ""),
                    "region": match.get("4. region", ""),
                    "currency": match.get("8. currency") or None,
                }
            )
        return results
