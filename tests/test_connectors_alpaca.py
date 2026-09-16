"""Alpaca connector tests (fixtures only, no network)."""

import json
from datetime import date
from decimal import Decimal

from frappe_investing.connectors import alpaca
from tests.fixtures.fakes import FakeResponse, FakeSession, load_fixture

KEY = "PKTESTKEY"
SECRET = "alpacaSecret000"


def make_connector(sandbox=False, session=None):
    sess = session or FakeSession()
    return alpaca.AlpacaConnector(KEY, SECRET, sandbox=sandbox, session=sess), sess


def test_base_urls_and_sandbox_default_off():
    connector, _ = make_connector()
    assert connector.base_url == "https://api.alpaca.markets"  # live by default
    paper, _ = make_connector(sandbox=True)
    assert paper.base_url == "https://paper-api.alpaca.markets"


def test_auth_headers():
    connector, sess = make_connector()
    sess.add("GET", "/v2/positions", FakeResponse(200, load_fixture("alpaca_positions.json")))
    connector.positions()
    headers = sess.calls[0]["kwargs"]["headers"]
    assert headers["APCA-API-KEY-ID"] == KEY
    assert headers["APCA-API-SECRET-KEY"] == SECRET


def test_account_normalizes():
    connector, sess = make_connector()
    sess.add("GET", "/v2/account", FakeResponse(200, load_fixture("alpaca_account.json")))
    account = connector.account()
    assert account == {"external_id": "PA1234567", "name": "Alpaca PA1234567", "currency": "USD"}


def test_positions_normalize_with_us_prefix():
    connector, sess = make_connector()
    sess.add("GET", "/v2/positions", FakeResponse(200, load_fixture("alpaca_positions.json")))
    positions = connector.positions()
    assert len(positions) == 2

    aapl = positions[0]
    # Alpaca symbols carry no exchange, so the exchange segment is the literal "US"
    assert aapl["security_key"] == "US:AAPL"
    assert aapl["qty"] == "12"
    assert aapl["avg_cost"] == "190.25"
    assert aapl["currency"] == "USD"
    assert aapl["market_price"] == "191.70"
    assert aapl["as_of"] == date.today().isoformat()
    assert Decimal(aapl["qty"]) == 12


def test_activities_paginate_via_next_page_token():
    connector, sess = make_connector()
    sess.push(FakeResponse(200, load_fixture("alpaca_activities_page1.json")))
    sess.push(FakeResponse(200, load_fixture("alpaca_activities_page2.json")))
    result = connector.activities(after="2026-08-01", until="2026-09-16")

    assert result["pages"] == 2
    assert result["truncated"] is False
    assert len(sess.calls) == 2
    assert "page_token" not in sess.calls[0]["kwargs"]["params"]
    assert sess.calls[1]["kwargs"]["params"]["page_token"] == "pg2"
    assert sess.calls[0]["kwargs"]["params"]["after"] == "2026-08-01"
    assert sess.calls[0]["kwargs"]["params"]["until"] == "2026-09-16"

    events = {e["source_ref"]: e for e in result["events"]}
    assert len(events) == 8

    buy = events["act-001"]
    assert buy["type"] == "Buy"
    assert buy["security_key"] == "US:AAPL"
    assert buy["qty"] == "10" and buy["price"] == "190.25"
    assert buy["date"] == "2026-09-15"  # from transaction_time
    assert buy["currency"] == "USD"

    sell = events["act-007"]
    assert sell["type"] == "Sell" and sell["qty"] == "4" and sell["price"] == "191.70"

    div = events["act-002"]
    assert div["type"] == "Dividend"
    assert div["security_key"] == "US:MSFT"
    assert div["gross"] == "22.50"  # gross = net_amount
    assert div["date"] == "2026-08-20"

    split = events["act-003"]
    assert split["type"] == "Split"
    assert split["security_key"] == "US:TSLA"
    # Alpaca reports only the additional shares; the old quantity is unknowable
    # here, so the ratio is skipped and the event is flagged for review.
    assert split["split_ratio"] is None
    assert split["meta"]["needs_review"] is True
    assert split["meta"]["additional_shares"] == "20"

    wht = events["act-004"]
    assert wht["type"] == "Fee"
    assert wht["amount"] == "2.00"
    assert wht["meta"]["needs_review"] is True

    deposit = events["act-005"]
    assert deposit["type"] == "Deposit" and deposit["amount"] == "5000.00"

    withdrawal = events["act-009"]
    assert withdrawal["type"] == "Withdrawal" and withdrawal["amount"] == "1000.00"

    interest = events["act-008"]
    assert interest["type"] == "Interest" and interest["gross"] == "1.23"

    for event in events.values():
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


def test_activities_unknown_types_are_skipped_with_reasons_never_dropped():
    connector, sess = make_connector()
    sess.push(FakeResponse(200, load_fixture("alpaca_activities_page1.json")))
    sess.push(FakeResponse(200, load_fixture("alpaca_activities_page2.json")))
    result = connector.activities()
    assert len(result["skipped"]) == 1
    skipped = result["skipped"][0]
    assert skipped["id"] == "act-006"
    assert skipped["activity_type"] == "FOOACT"
    assert "FOOACT" in skipped["reason"]


def test_activities_pagination_hard_cap_at_50_pages():
    connector, sess = make_connector()
    page = json.dumps({"activities": [], "next_page_token": "again"})
    sess.add("GET", "/v2/account/activities", FakeResponse(200, page))
    result = connector.activities()
    assert len(sess.calls) == 50
    assert result["truncated"] is True
    assert result["pages"] == 50


def test_activities_bare_array_response_is_single_page():
    connector, sess = make_connector()
    body = json.dumps([json.loads(load_fixture("alpaca_activities_page2.json"))["activities"][0]])
    sess.push(FakeResponse(200, body))
    result = connector.activities()
    assert result["pages"] == 1
    assert result["truncated"] is False
    assert len(result["events"]) == 1


def test_capabilities():
    connector, _ = make_connector()
    assert connector.capabilities() == {
        "accounts": True,
        "positions": True,
        "trades": True,
        "income": True,
        "corporate_actions": True,
        "history": True,
        "quotes": False,
    }


def test_module_docstring_documents_exchange_and_split_limits():
    doc = alpaca.__doc__
    assert "exchange" in doc  # Alpaca symbols lack an exchange segment
    assert "needs_review" in doc  # split ratio limitation is documented
