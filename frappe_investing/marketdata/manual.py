"""Manual quotes: the caller supplies the prices, we normalize them.

Free-tier EOD fallback — type the close from a broker statement or
exchange website. No network calls are ever made; quotes absent from the
store are skipped, never fabricated.
"""

from .base import ProviderBase, day_str


class ManualProvider(ProviderBase):
    name = "manual"
    supported = frozenset({"daily_prices"})

    def __init__(self, quotes=(), session=None):
        super().__init__(session=session)
        self._quotes = {}
        for quote in quotes:
            self.add(quote["security_key"], quote["day"], quote["close"], quote.get("currency"))

    def add(self, security_key, day, close, currency=None):
        key = (security_key, day_str(day))
        self._quotes[key] = {
            "security_key": security_key,
            "day": key[1],
            "close": str(close),
            "currency": currency,
        }

    def daily_prices(self, security_keys, day):
        target_day = day_str(day)
        return [
            dict(self._quotes[(key, target_day)])
            for key in security_keys
            if (key, target_day) in self._quotes
        ]

    def search(self, query):
        return []
