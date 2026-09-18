"""Zerodha Kite Connect v3 connector tests (fixtures only, no network)."""

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from frappe_investing.connectors import zerodha
from frappe_investing.connectors.base import BrokerError
from tests.fixtures.fakes import FakeResponse, FakeSession, load_fixture

API_KEY = "kitekey123"
API_SECRET = "kitesecret456"
REQUEST_TOKEN = "reqtok789"
ACCESS_TOKEN = "acctok000"


def make_connector(session=None):
    sess = session or FakeSession()
    return zerodha.ZerodhaConnector(API_KEY, API_SECRET, access_token=ACCESS_TOKEN, session=sess), sess


def test_login_url():
    connector = zerodha.ZerodhaConnector(API_KEY, API_SECRET, session=FakeSession())
    assert connector.login_url() == "https://kite.zerodha.com/connect/login?api_key=kitekey123&v=3"


def test_exchange_token_posts_signed_checksum_and_stores_access_token():
    sess = FakeSession()
    sess.add("POST", "/session/token", FakeResponse(200, load_fixture("zerodha_token.json")))
    connector = zerodha.ZerodhaConnector(API_KEY, API_SECRET, session=sess)
    token = connector.exchange_token(REQUEST_TOKEN)
    assert token == "acc3ssT0k3nValue"
    assert connector.access_token == "acc3ssT0k3nValue"

    call = sess.calls[0]
    assert call["method"] == "POST"
    form = call["kwargs"]["data"]
    expected = hashlib.sha256((API_KEY + REQUEST_TOKEN + API_SECRET).encode("utf-8")).hexdigest()
    assert form == {"api_key": API_KEY, "request_token": REQUEST_TOKEN, "checksum": expected}
    assert call["kwargs"]["headers"]["X-Kite-Version"] == "3"


def test_exchange_token_failure_raises_auth_error():
    sess = FakeSession()
    sess.add("POST", "/session/token", FakeResponse(200, '{"status": "error"}'))
    connector = zerodha.ZerodhaConnector(API_KEY, API_SECRET, session=sess)
    with pytest.raises(BrokerError) as err:
        connector.exchange_token(REQUEST_TOKEN)
    assert err.value.code == "auth"


def test_auth_headers_use_token_scheme():
    connector, sess = make_connector()
    sess.add("GET", "/portfolio/holdings", FakeResponse(200, load_fixture("zerodha_holdings.json")))
    connector.holdings()
    headers = sess.calls[0]["kwargs"]["headers"]
    assert headers["X-Kite-Version"] == "3"
    assert headers["Authorization"] == f"token {API_KEY}:{ACCESS_TOKEN}"


def test_holdings_normalize_to_position_dicts():
    connector, sess = make_connector()
    sess.add("GET", "/portfolio/holdings", FakeResponse(200, load_fixture("zerodha_holdings.json")))
    positions = connector.holdings()
    assert len(positions) == 2

    reliance = positions[0]
    assert reliance["security_key"] == "NSE:RELIANCE"  # exchange:tradingsymbol
    assert reliance["qty"] == "10"
    assert reliance["avg_cost"] == "2450.5"
    assert reliance["currency"] == "INR"
    assert reliance["market_price"] == "2501.75"
    assert reliance["as_of"] == date.today().isoformat()
    # every numeric field must be Decimal-parseable
    assert Decimal(reliance["qty"]) == 10
    assert Decimal(reliance["avg_cost"]) == Decimal("2450.5")

    tcs = positions[1]
    assert tcs["security_key"] == "BSE:TCS"
    assert tcs["qty"] == "5"
    assert tcs["avg_cost"] == "3100.0"
    assert tcs["market_price"] == "3150.25"


def test_todays_orders_returns_all_statuses():
    connector, sess = make_connector()
    sess.add("GET", "/orders", FakeResponse(200, load_fixture("zerodha_orders.json")))
    orders = connector.todays_orders()
    assert len(orders) == 4
    assert orders[0]["order_id"] == "100000000000001"
    assert orders[0]["security_key"] == "NSE:RELIANCE"
    assert orders[0]["side"] == "BUY"
    assert orders[2]["status"] == "OPEN"
    assert orders[3]["status"] == "REJECTED"


def test_todays_trades_emit_buy_sell_events_for_complete_orders_only():
    connector, sess = make_connector()
    sess.add("GET", "/orders", FakeResponse(200, load_fixture("zerodha_orders.json")))
    sess.add(
        "GET",
        "/orders/100000000000001/trades",
        FakeResponse(200, load_fixture("zerodha_trades_100000000000001.json")),
    )
    sess.add(
        "GET",
        "/orders/100000000000002/trades",
        FakeResponse(200, load_fixture("zerodha_trades_100000000000002.json")),
    )

    result = connector.todays_trades()
    assert result["truncated"] is False
    events = result["events"]
    assert len(events) == 3

    buy = events[0]
    assert buy["type"] == "Buy"
    assert buy["security_key"] == "NSE:RELIANCE"
    assert buy["qty"] == "10"
    assert buy["price"] == "2450.5"
    assert buy["currency"] == "INR"
    assert buy["date"] == "2026-09-16"
    assert buy["source_ref"] == "50000001"
    assert buy["meta"]["order_id"] == "100000000000001"
    assert Decimal(buy["qty"]) == 10 and Decimal(buy["price"]) == Decimal("2450.5")

    sells = events[1:]
    assert [e["type"] for e in sells] == ["Sell", "Sell"]
    assert [e["source_ref"] for e in sells] == ["50000002", "50000003"]
    assert sells[0]["qty"] == "3" and sells[0]["price"] == "3120.4"
    assert sells[1]["qty"] == "2" and sells[1]["price"] == "3121.0"

    # OPEN and REJECTED orders never fetch their trade legs
    trade_calls = [c for c in sess.calls if c["url"].endswith("/trades")]
    assert len(trade_calls) == 2


def test_event_dicts_carry_full_normalized_contract_keys():
    connector, sess = make_connector()
    sess.add("GET", "/orders", FakeResponse(200, load_fixture("zerodha_orders.json")))
    sess.add(
        "GET",
        "/orders/100000000000001/trades",
        FakeResponse(200, load_fixture("zerodha_trades_100000000000001.json")),
    )
    sess.add(
        "GET",
        "/orders/100000000000002/trades",
        FakeResponse(200, load_fixture("zerodha_trades_100000000000002.json")),
    )
    for event in connector.todays_trades()["events"]:
        for key in (
            "type",
            "security_key",
            "qty",
            "price",
            "amount",
            "gross",
            "fees",
            "taxes",
            "currency",
            "date",
            "source_ref",
            "meta",
        ):
            assert key in event


def test_instruments_parses_kite_csv_master():
    connector, sess = make_connector()
    sess.add("GET", "/instruments/NSE", FakeResponse(200, load_fixture("zerodha_instruments.csv")))
    rows = connector.instruments("NSE")
    assert len(rows) == 3

    equity = rows[0]
    assert equity["instrument_token"] == "738561"
    assert equity["tradingsymbol"] == "RELIANCE"
    assert equity["name"] == "RELIANCE INDUSTRIES"
    assert equity["instrument_type"] == "EQ"
    assert equity["expiry"] == ""
    assert equity["strike"] == "0"
    assert equity["tick_size"] == "0.05"
    assert equity["lot_size"] == "1"
    assert equity["exchange"] == "NSE"
    assert equity["security_key"] == "NSE:RELIANCE"

    option = rows[2]
    assert option["instrument_type"] == "CE"
    assert option["expiry"] == "2026-09-24"
    assert option["strike"] == "1500"
    assert option["security_key"] == "NFO:INFY26SEP1500CE"


def test_capabilities_are_honest_about_kite_limits():
    connector, _ = make_connector()
    assert connector.capabilities() == {
        "accounts": False,
        "positions": True,
        "trades": True,
        "income": False,
        "corporate_actions": False,
        "history": False,
        "quotes": False,
    }


def test_module_docstring_documents_no_history_no_dividends():
    doc = zerodha.__doc__
    assert "no historical trades" in doc
    assert "no dividend feed" in doc


def test_api_errors_are_sanitized_and_never_leak_key_or_secret():
    connector, sess = make_connector()
    sess.add("GET", "/portfolio/holdings", FakeResponse(403, load_fixture("zerodha_error.json")))
    with pytest.raises(BrokerError) as err:
        connector.holdings()
    assert API_KEY not in str(err.value)
    assert API_SECRET not in str(err.value)
    assert ACCESS_TOKEN not in str(err.value)
    assert err.value.code == "auth"


def test_positions_uses_net_rows_not_day_rows():
    connector, sess = make_connector()
    sess.add("GET", "/portfolio/positions", FakeResponse(200, load_fixture("zerodha_positions.json")))
    positions = connector.positions()
    assert len(positions) == 2

    reliance = positions[0]
    assert reliance["security_key"] == "NSE:RELIANCE"
    assert reliance["qty"] == "15"
    assert reliance["avg_cost"] == "2440.0"
    assert reliance["currency"] == "INR"
    assert reliance["market_price"] == "2505.5"
    assert reliance["as_of"] == date.today().isoformat()
    assert Decimal(reliance["qty"]) == 15

    tcs = positions[1]
    assert tcs["security_key"] == "BSE:TCS"
    assert tcs["qty"] == "8"
    assert tcs["market_price"] == "3155.0"
