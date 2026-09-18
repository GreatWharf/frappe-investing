"""Market-data provider tests: base, Stooq, Alpha Vantage, manual."""

from datetime import date
from decimal import Decimal

import pytest

from frappe_investing.marketdata import alphavantage, manual, stooq
from frappe_investing.marketdata.base import MarketDataError, ProviderBase
from tests.fixtures.fakes import FakeResponse, FakeSession, load_fixture


def test_base_provider_capabilities_default_off_and_methods_abstract():
    provider = ProviderBase()
    assert provider.capabilities() == {"daily_prices": False, "search": False}
    with pytest.raises(NotImplementedError):
        provider.daily_prices(["US:AAPL"], date(2026, 9, 15))
    with pytest.raises(NotImplementedError):
        provider.search("apple")


def test_marketdata_error_carries_code():
    err = MarketDataError("boom", code="rate_limited")
    assert err.code == "rate_limited"
    assert str(err) == "boom"


# --- Stooq -----------------------------------------------------------------


def test_stooq_daily_prices_parse_csv():
    sess = FakeSession()
    sess.add("GET", "stooq.com", FakeResponse(200, load_fixture("stooq_daily.csv")))
    provider = stooq.StooqProvider(session=sess)
    quotes = provider.daily_prices(["AAPL.US"], date(2026, 9, 15))

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote == {"security_key": "AAPL.US", "day": "2026-09-15", "close": "191.70", "currency": None}
    assert Decimal(quote["close"]) == Decimal("191.70")

    params = sess.calls[0]["kwargs"]["params"]
    assert params == {"s": "AAPL.US", "d1": "20260915", "d2": "20260915", "i": "d"}


def test_stooq_symbol_map_is_caller_supplied():
    sess = FakeSession()
    sess.add("GET", "stooq.com", FakeResponse(200, load_fixture("stooq_daily.csv")))
    provider = stooq.StooqProvider(symbol_map={"NSE:RELIANCE": "RELIANCE.NS"}, session=sess)
    quotes = provider.daily_prices(["NSE:RELIANCE"], "2026-09-15")
    assert sess.calls[0]["kwargs"]["params"]["s"] == "RELIANCE.NS"
    assert quotes[0]["security_key"] == "NSE:RELIANCE"  # quote keyed by OUR security key
    assert quotes[0]["currency"] is None  # stooq CSV has no currency; caller applies Security's


def test_stooq_rate_limit_text_maps_to_rate_limited():
    sess = FakeSession()
    sess.add("GET", "stooq.com", FakeResponse(200, load_fixture("stooq_rate_limit.txt")))
    provider = stooq.StooqProvider(session=sess)
    with pytest.raises(MarketDataError) as err:
        provider.daily_prices(["AAPL.US"], date(2026, 9, 15))
    assert err.value.code == "rate_limited"


def test_stooq_unknown_symbol_is_skipped_not_fabricated():
    sess = FakeSession()
    sess.add("GET", "stooq.com", FakeResponse(200, "Date,Open,High,Low,Close,Volume\n"))
    provider = stooq.StooqProvider(session=sess)
    assert provider.daily_prices(["NOPE.US"], date(2026, 9, 15)) == []


def test_stooq_has_no_search():
    provider = stooq.StooqProvider(session=FakeSession())
    assert provider.capabilities() == {"daily_prices": True, "search": False}
    assert provider.search("apple") == []


def test_stooq_http_error_is_market_data_error_not_broker_error():
    """A non-2xx from stooq must surface as MarketDataError.

    sync_service.refresh_prices catches MarketDataError per security and
    continues; a raw BrokerError escaping the adapter 500s the whole
    refresh (seen live when stooq answered 404).
    """
    sess = FakeSession()
    sess.add("GET", "stooq.com", FakeResponse(404, "not found"))
    provider = stooq.StooqProvider(session=sess)
    with pytest.raises(MarketDataError) as err:
        provider.daily_prices(["AAPL.US"], date(2026, 9, 15))
    assert err.value.code == "not_found"


def test_stooq_anti_bot_challenge_maps_to_blocked():
    """Stooq now gates anonymous CSV behind a JavaScript proof-of-work
    challenge (HTTP 200 with an HTML page, not CSV). Detect the challenge
    and say so plainly instead of silently returning no rows.
    """
    challenge = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        "<noscript>This site requires JavaScript to verify your browser.</noscript>"
        '<script>fetch("/__verify",{method:"POST"})</script></body></html>'
    )
    sess = FakeSession()
    sess.add("GET", "stooq.com", FakeResponse(200, challenge))
    provider = stooq.StooqProvider(session=sess)
    with pytest.raises(MarketDataError) as err:
        provider.daily_prices(["AAPL.US"], date(2026, 9, 15))
    assert err.value.code == "blocked"


# --- Alpha Vantage ----------------------------------------------------------


def test_alphavantage_daily_prices_parse_compact_series():
    sess = FakeSession()
    sess.add("GET", "alphavantage.co", FakeResponse(200, load_fixture("alphavantage_daily.json")))
    provider = alphavantage.AlphaVantageProvider("demokey", session=sess)
    quotes = provider.daily_prices(["US:AAPL"], date(2026, 9, 15))

    assert quotes == [{"security_key": "US:AAPL", "day": "2026-09-15", "close": "191.70", "currency": None}]
    params = sess.calls[0]["kwargs"]["params"]
    assert params["function"] == "TIME_SERIES_DAILY"
    assert params["symbol"] == "AAPL"  # exchange prefix stripped
    assert params["outputsize"] == "compact"
    assert params["apikey"] == "demokey"


def test_alphavantage_symbol_map_for_non_us_listings():
    sess = FakeSession()
    sess.add("GET", "alphavantage.co", FakeResponse(200, load_fixture("alphavantage_daily.json")))
    provider = alphavantage.AlphaVantageProvider(
        "demokey", symbol_map={"NSE:RELIANCE": "RELIANCE.BSE"}, session=sess
    )
    quotes = provider.daily_prices(["NSE:RELIANCE"], date(2026, 9, 15))
    assert sess.calls[0]["kwargs"]["params"]["symbol"] == "RELIANCE.BSE"
    assert quotes[0]["security_key"] == "NSE:RELIANCE"


def test_alphavantage_missing_day_is_skipped():
    sess = FakeSession()
    sess.add("GET", "alphavantage.co", FakeResponse(200, load_fixture("alphavantage_daily.json")))
    provider = alphavantage.AlphaVantageProvider("demokey", session=sess)
    assert provider.daily_prices(["US:AAPL"], date(2020, 1, 1)) == []


def test_alphavantage_note_and_information_mean_rate_limited():
    for fixture in ("alphavantage_note.json", "alphavantage_information.json"):
        sess = FakeSession()
        sess.add("GET", "alphavantage.co", FakeResponse(200, load_fixture(fixture)))
        provider = alphavantage.AlphaVantageProvider("demoLEAKkey123", session=sess)
        with pytest.raises(MarketDataError) as err:
            provider.daily_prices(["US:AAPL"], date(2026, 9, 15))
        assert err.value.code == "rate_limited"
        assert "demoLEAKkey123" not in str(err.value)  # body (and key) never echoed


def test_alphavantage_search_maps_best_matches():
    sess = FakeSession()
    sess.add("GET", "alphavantage.co", FakeResponse(200, load_fixture("alphavantage_search.json")))
    provider = alphavantage.AlphaVantageProvider("demokey", session=sess)
    results = provider.search("apple")
    assert sess.calls[0]["kwargs"]["params"]["function"] == "SYMBOL_SEARCH"
    assert len(results) == 2
    assert results[0]["security_key"] == "US:AAPL"
    assert results[0]["name"] == "Apple Inc"
    assert results[0]["currency"] == "USD"
    assert results[1]["security_key"] == "AAPL.MEX"  # dotted symbols kept verbatim
    assert results[1]["currency"] == "MXN"


def test_alphavantage_capabilities_and_terms_docstring():
    provider = alphavantage.AlphaVantageProvider("demokey", session=FakeSession())
    assert provider.capabilities() == {"daily_prices": True, "search": True}
    doc = alphavantage.__doc__
    assert "non-commercial" in doc  # default terms are personal/non-commercial
    assert "opt-in" in doc  # adapter is opt-in with the user's own key


# --- Manual -----------------------------------------------------------------


def test_manual_provider_round_trips_caller_supplied_quotes():
    provider = manual.ManualProvider(
        quotes=[
            {"security_key": "NSE:RELIANCE", "day": "2026-09-15", "close": "2501.75", "currency": "INR"},
            {"security_key": "US:AAPL", "day": "2026-09-15", "close": "191.70", "currency": "USD"},
        ]
    )
    quotes = provider.daily_prices(["NSE:RELIANCE", "US:AAPL", "US:MSFT"], date(2026, 9, 15))
    assert quotes == [
        {"security_key": "NSE:RELIANCE", "day": "2026-09-15", "close": "2501.75", "currency": "INR"},
        {"security_key": "US:AAPL", "day": "2026-09-15", "close": "191.70", "currency": "USD"},
    ]  # missing keys are skipped, never fabricated
    assert provider.search("anything") == []
    assert provider.capabilities() == {"daily_prices": True, "search": False}
