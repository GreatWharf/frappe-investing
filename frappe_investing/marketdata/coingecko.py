"""CoinGecko market data for crypto.

Crypto counts toward the license's asset-class limit, so this adapter
requires an injected ``gate`` callable: ``gate("crypto")`` must return True
or every call raises ``MarketDataError("Crypto requires a tier with more
asset classes")``. The public API needs no key.

* ``/simple/price`` returns the CURRENT price only. ``daily_prices`` serves
  today's quote and raises ``code="unsupported"`` for any past day — we
  never fabricate historical prices.
* ``/coins/markets`` backs ``search`` (top 250 by market cap, filtered
  client-side, 25 results max).
* security_key convention: ``CRYPTO:BTC``. CoinGecko ids are not tickers,
  so a small built-in map covers the majors; ``symbol_map`` overrides or
  extends it; unknown symbols fall back to the lowercased ticker as the id
  guess and simply return no quote when CoinGecko does not know the id.
"""

from datetime import date

from ..connectors.base import http_json
from .base import MarketDataError, ProviderBase, day_str

PRO_TIER_MESSAGE = "Crypto requires a tier with more asset classes"

BUILTIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "USDC": "usd-coin",
    "USDT": "tether",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "DOT": "polkadot",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "LINK": "chainlink",
}
MAX_SEARCH_RESULTS = 25


class CoinGeckoProvider(ProviderBase):
    name = "coingecko"
    supported = frozenset({"daily_prices", "search"})
    BASE = "https://api.coingecko.com/api/v3"

    def __init__(self, gate, symbol_map=None, vs_currency="usd", session=None):
        super().__init__(session=session)
        self.gate = gate
        self.symbol_map = {key.upper(): value for key, value in dict(symbol_map or {}).items()}
        self.vs_currency = vs_currency

    def _check_tier(self):
        if self.gate is None or not self.gate("crypto"):
            raise MarketDataError(PRO_TIER_MESSAGE, code="tier_gated")

    def _coin_id(self, security_key):
        symbol = security_key.split(":", 1)[1] if ":" in security_key else security_key
        symbol = symbol.upper()
        return self.symbol_map.get(symbol, BUILTIN_IDS.get(symbol, symbol.lower()))

    def daily_prices(self, security_keys, day):
        """Current-day quotes only; past days raise code="unsupported" (no fabrication)."""
        self._check_tier()
        target_day = day_str(day)
        today = date.today().isoformat()
        if target_day != today:
            raise MarketDataError(
                f"CoinGecko adapter serves current prices only; no historical EOD for {target_day}",
                code="unsupported",
            )
        coin_ids = {key: self._coin_id(key) for key in security_keys}
        payload = http_json(
            self.session,
            "GET",
            f"{self.BASE}/simple/price",
            params={"ids": ",".join(sorted(set(coin_ids.values()))), "vs_currencies": self.vs_currency},
        )
        quotes = []
        for key, coin_id in coin_ids.items():
            price = payload.get(coin_id, {}).get(self.vs_currency) if isinstance(payload, dict) else None
            if price is None:
                continue  # unknown id: skipped, never fabricated
            quotes.append(
                {"security_key": key, "day": today, "close": str(price), "currency": self.vs_currency.upper()}
            )
        return quotes

    def search(self, query):
        """Search the top-250 market list client-side -> CRYPTO:SYM keys."""
        self._check_tier()
        payload = http_json(
            self.session,
            "GET",
            f"{self.BASE}/coins/markets",
            params={
                "vs_currency": self.vs_currency,
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": "false",
            },
        )
        needle = query.strip().lower()
        results = []
        for row in payload or []:
            haystack = f"{row.get('id', '')} {row.get('symbol', '')} {row.get('name', '')}".lower()
            if needle in haystack:
                results.append(
                    {
                        "security_key": f"CRYPTO:{str(row.get('symbol', '')).upper()}",
                        "name": row.get("name", ""),
                        "provider_id": row.get("id", ""),
                        "currency": self.vs_currency.upper(),
                    }
                )
            if len(results) >= MAX_SEARCH_RESULTS:
                break
        return results
