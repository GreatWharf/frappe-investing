"""Stooq free end-of-day prices (no API key).

GET https://stooq.com/q/d/l/?s={symbol}&d1=&d2=&i=d returns CSV with
Date,Open,High,Low,Close,Volume. One request per symbol.

* The caller maps Security -> stooq symbol via ``symbol_map`` (e.g.
  ``{"NSE:RELIANCE": "RELIANCE.NS"}``); unmapped keys are sent as-is,
  which works when keys already use stooq's format (``AAPL.US``).
* Stooq's CSV carries no currency, so quotes come back with
  ``currency=None`` — the caller applies the Security's currency.
* Exceeding the anonymous daily hit limit returns plain text starting
  with "Exceeded the daily hits limit" -> MarketDataError rate_limited.
* No search endpoint: ``search()`` returns [].
"""

import csv
import io

from ..connectors.base import BrokerError, http_request
from .base import MarketDataError, ProviderBase, day_str

_RATE_LIMIT_PREFIX = "Exceeded the daily hits limit"
_ANTI_BOT_MARKERS = ("__verify", "requires JavaScript to verify")


class StooqProvider(ProviderBase):
    name = "stooq"
    supported = frozenset({"daily_prices"})
    URL = "https://stooq.com/q/d/l/"

    def __init__(self, symbol_map=None, session=None):
        super().__init__(session=session)
        self.symbol_map = dict(symbol_map or {})

    def daily_prices(self, security_keys, day):
        target_day = day_str(day)
        compact = target_day.replace("-", "")
        quotes = []
        for key in security_keys:
            try:
                body = http_request(
                    self.session,
                    "GET",
                    self.URL,
                    params={"s": self.symbol_map.get(key, key), "d1": compact, "d2": compact, "i": "d"},
                )
            except BrokerError as exc:
                # Translate into the market-data error type so callers
                # (refresh_prices) log and continue instead of 500ing.
                raise MarketDataError(f"Stooq request failed: {exc}", code=exc.code) from exc
            text = body.decode("utf-8", "replace").strip()
            if any(marker in text for marker in _ANTI_BOT_MARKERS):
                # Stooq gates anonymous CSV behind a JavaScript proof-of-work
                # challenge (HTTP 200 with an HTML page, never parseable as
                # CSV). Name it plainly rather than returning "no data".
                raise MarketDataError(
                    "Stooq is blocking automated access with an anti-bot challenge; "
                    "choose another price provider or import prices via CSV",
                    code="blocked",
                )
            if text.startswith(_RATE_LIMIT_PREFIX):
                raise MarketDataError("Stooq daily hit limit exceeded", code="rate_limited")
            rows = [row for row in csv.DictReader(io.StringIO(text)) if row.get("Close")]
            if not rows:
                continue  # unknown symbol or non-trading day: skipped, never fabricated
            row = rows[-1]
            quotes.append(
                {
                    "security_key": key,
                    "day": row.get("Date", target_day),
                    "close": row["Close"],
                    "currency": None,
                }
            )
        return quotes

    def search(self, query):
        return []
