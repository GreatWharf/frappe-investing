"""CoinGecko adapter tests: Pro-tier gating and no fabricated pricing."""

from datetime import date

import pytest

from frappe_investing.marketdata import coingecko
from frappe_investing.marketdata.base import MarketDataError
from tests.fixtures.fakes import FakeResponse, FakeSession, load_fixture

PRO_GATE = lambda feature: feature == "crypto"  # noqa: E731


def make_provider(gate=PRO_GATE, session=None):
    return coingecko.CoinGeckoProvider(gate=gate, session=session or FakeSession())


def test_gate_denied_blocks_prices_and_search_with_pro_tier_message():
    provider = make_provider(gate=lambda feature: False)
    with pytest.raises(MarketDataError, match="Crypto requires the Pro tier"):
        provider.daily_prices(["CRYPTO:BTC"], date.today())
    with pytest.raises(MarketDataError, match="Crypto requires the Pro tier"):
        provider.search("bitcoin")


def test_missing_gate_blocks_everything():
    provider = coingecko.CoinGeckoProvider(gate=None, session=FakeSession())
    with pytest.raises(MarketDataError, match="Crypto requires the Pro tier"):
        provider.daily_prices(["CRYPTO:BTC"], date.today())


def test_gate_is_checked_with_the_crypto_feature_flag():
    seen = []

    def gate(feature):
        seen.append(feature)
        return True

    sess = FakeSession()
    sess.add("GET", "simple/price", FakeResponse(200, load_fixture("coingecko_simple_price.json")))
    make_provider(gate=gate, session=sess).daily_prices(["CRYPTO:BTC"], date.today())
    assert seen == ["crypto"]


def test_current_day_prices_from_simple_price():
    sess = FakeSession()
    sess.add("GET", "simple/price", FakeResponse(200, load_fixture("coingecko_simple_price.json")))
    provider = make_provider(session=sess)
    quotes = provider.daily_prices(["CRYPTO:BTC", "CRYPTO:ETH", "CRYPTO:ZZZ"], date.today())

    assert quotes == [
        {
            "security_key": "CRYPTO:BTC",
            "day": date.today().isoformat(),
            "close": "112345.67",
            "currency": "USD",
        },
        {
            "security_key": "CRYPTO:ETH",
            "day": date.today().isoformat(),
            "close": "4567.89",
            "currency": "USD",
        },
    ]  # unknown coin id is skipped, never fabricated

    params = sess.calls[0]["kwargs"]["params"]
    assert params["ids"] == "bitcoin,ethereum,zzz"  # built-in ticker map + lowercase fallback
    assert params["vs_currencies"] == "usd"


def test_historical_days_are_refused_not_fabricated():
    sess = FakeSession()
    provider = make_provider(session=sess)
    with pytest.raises(MarketDataError) as err:
        provider.daily_prices(["CRYPTO:BTC"], date(2020, 1, 1))
    assert err.value.code == "unsupported"
    assert sess.calls == []  # no request was even attempted


def test_search_filters_coins_markets():
    sess = FakeSession()
    sess.add("GET", "coins/markets", FakeResponse(200, load_fixture("coingecko_markets.json")))
    provider = make_provider(session=sess)

    results = provider.search("eth")
    assert results == [
        {"security_key": "CRYPTO:ETH", "name": "Ethereum", "provider_id": "ethereum", "currency": "USD"}
    ]

    results = provider.search("bit")
    assert [r["security_key"] for r in results] == ["CRYPTO:BTC"]

    assert provider.search("nonexistent-coin") == []


def test_symbol_map_overrides_builtin_ids():
    sess = FakeSession()
    sess.add("GET", "simple/price", FakeResponse(200, '{"wrapped-bitcoin": {"usd": 112000.0}}'))
    provider = coingecko.CoinGeckoProvider(
        gate=PRO_GATE, symbol_map={"WBTC": "wrapped-bitcoin"}, session=sess
    )
    quotes = provider.daily_prices(["CRYPTO:WBTC"], date.today())
    assert sess.calls[0]["kwargs"]["params"]["ids"] == "wrapped-bitcoin"
    assert quotes[0]["close"] == "112000.0"


def test_capabilities():
    assert make_provider().capabilities() == {"daily_prices": True, "search": True}
