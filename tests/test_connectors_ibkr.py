"""IBKR Flex Web Service connector tests (fixtures only, no network)."""

import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

from frappe_investing.connectors import ibkr_flex
from frappe_investing.connectors.base import BrokerError
from tests.fixtures.fakes import FakeResponse, FakeSession, load_fixture

TOKEN = "tok3nXYZ123"
QUERY_ID = "Q_FINV_01"


def make_connector(session=None, sleeps=None):
    sess = session or FakeSession()
    sleep_log = sleeps if sleeps is not None else []
    connector = ibkr_flex.IBKRFlexConnector(TOKEN, session=sess, sleeper=sleep_log.append, poll_interval=0.5)
    return connector, sess, sleep_log


def test_fetch_runs_two_step_flow_with_token_in_params():
    connector, sess, sleeps = make_connector()
    sess.add("POST", "SendRequest", FakeResponse(200, load_fixture("ibkr_send_ok.xml")))
    sess.push(FakeResponse(200, load_fixture("ibkr_pending.xml")))
    sess.push(FakeResponse(200, load_fixture("ibkr_statement.xml")))

    result = connector.fetch(QUERY_ID)
    assert result["events"] and result["errors"]

    send_call = sess.calls[0]
    assert send_call["method"] == "POST"
    assert send_call["url"].startswith("https://ndcdyn.interactivebrokers.com/")
    assert send_call["kwargs"]["params"] == {"t": TOKEN, "q": QUERY_ID, "v": "3"}

    poll_calls = sess.calls[1:]
    assert len(poll_calls) == 2
    assert poll_calls[0]["kwargs"]["params"] == {"t": TOKEN, "q": "1234567890", "v": "3"}
    assert sleeps == [0.5]  # one pending response -> one backoff sleep


def test_send_request_rejection_sanitizes_token():
    connector, sess, _ = make_connector()
    sess.add("POST", "SendRequest", FakeResponse(200, load_fixture("ibkr_send_error.xml")))
    with pytest.raises(BrokerError) as err:
        connector.fetch(QUERY_ID)
    assert err.value.code == "flex_error"
    assert TOKEN not in str(err.value)  # token is scrubbed even from IBKR's own error text


def test_polling_gives_up_after_max_polls():
    connector, sess, sleeps = make_connector()
    sess.add("POST", "SendRequest", FakeResponse(200, load_fixture("ibkr_send_ok.xml")))
    for _ in range(5):
        sess.push(FakeResponse(200, load_fixture("ibkr_pending.xml")))
    with pytest.raises(BrokerError) as err:
        connector.fetch(QUERY_ID)
    assert err.value.code == "timeout"
    assert sleeps == [0.5, 1.0, 1.5, 2.0]  # linear backoff, no sleep after the last attempt


def test_parse_statement_trades():
    result = ibkr_flex.IBKRFlexConnector.parse_statement(load_fixture("ibkr_statement.xml"))
    events = {e["source_ref"]: e for e in result["events"]}

    buy = events["987654321"]
    assert buy["type"] == "Buy"
    assert buy["security_key"] == "ISIN:US0378331005"  # ISIN wins when present
    assert buy["qty"] == "10"
    assert buy["price"] == "190.25"
    assert buy["fees"] == "1.00"  # commission arrives negative, normalized to a positive charge
    assert buy["taxes"] == "0"
    assert buy["currency"] == "USD"
    assert buy["date"] == "2026-09-10"  # yyyyMMdd normalized
    assert buy["meta"]["exchange"] == "NASDAQ"
    assert Decimal(buy["qty"]) == 10

    sell = events["987654322"]
    assert sell["type"] == "Sell"
    assert sell["security_key"] == "SMART:MSF"  # no ISIN -> SMART:SYMBOL
    assert sell["qty"] == "5"  # Flex sells carry negative quantities; normalized positive
    assert sell["fees"] == "0.75"


def test_parse_statement_dividend_with_merged_withholding():
    result = ibkr_flex.IBKRFlexConnector.parse_statement(load_fixture("ibkr_statement.xml"))
    events = {e["source_ref"]: e for e in result["events"]}

    dividend = events["555000001"]
    assert dividend["type"] == "Dividend"
    assert dividend["security_key"] == "ISIN:US0378331005"
    assert dividend["gross"] == "24.50"
    assert dividend["taxes"] == "3.68"  # same-day same-security WHT merged in
    assert dividend["date"] == "2026-09-12"
    assert Decimal(dividend["gross"]) - Decimal(dividend["taxes"]) == Decimal("20.82")


def test_parse_statement_unmatched_withholding_becomes_reviewable_fee():
    result = ibkr_flex.IBKRFlexConnector.parse_statement(load_fixture("ibkr_statement.xml"))
    events = {e["source_ref"]: e for e in result["events"]}

    fee = events["555000003"]
    assert fee["type"] == "Fee"
    assert fee["amount"] == "8.00"
    assert fee["currency"] == "CHF"
    assert fee["security_key"] == "ISIN:CH0038863350"
    assert fee["meta"]["needs_review"] is True


def test_parse_statement_cash_rows_and_unknown_type_reporting():
    result = ibkr_flex.IBKRFlexConnector.parse_statement(load_fixture("ibkr_statement.xml"))
    events = {e["source_ref"]: e for e in result["events"]}

    deposit = events["555000004"]
    assert deposit["type"] == "Deposit" and deposit["amount"] == "5000.00"
    assert deposit["security_key"] is None

    fee = events["555000005"]
    assert fee["type"] == "Fee" and fee["amount"] == "2.50"

    # "Mystery Cash" is an unknown cash type: reported, never silently dropped
    assert any("Mystery Cash" in e["message"] for e in result["errors"])


def test_parse_statement_corporate_actions():
    result = ibkr_flex.IBKRFlexConnector.parse_statement(load_fixture("ibkr_statement.xml"))
    events = {e["source_ref"]: e for e in result["events"]}

    split = events["777000001"]
    assert split["type"] == "Split"
    assert split["security_key"] == "ISIN:US88160R1014"
    assert split["split_ratio"] == "3"
    assert split["date"] == "2026-08-25"
    assert Decimal(split["split_ratio"]) == 3

    spinoff = events["777000002"]
    assert spinoff["type"] == "Spin-off"
    assert spinoff["security_key"] == "SMART:ABC"
    assert spinoff["meta"]["needs_review"] is True


def test_parse_statement_reports_unparsed_sections_and_never_drops_rows():
    result = ibkr_flex.IBKRFlexConnector.parse_statement(load_fixture("ibkr_statement.xml"))
    assert len(result["events"]) == 8  # 2 trades + dividend + deposit + 2 fees + split + spinoff
    assert any("SomeFutureSection" in e["message"] for e in result["errors"])
    assert len(result["errors"]) == 2


def test_parse_statement_size_cap():
    with pytest.raises(BrokerError) as err:
        ibkr_flex.IBKRFlexConnector.parse_statement("<" + "x" * ibkr_flex.MAX_XML_BYTES)
    assert err.value.code == "too_large"


def test_parse_statement_rejects_wrong_document_and_bad_xml():
    with pytest.raises(BrokerError) as err:
        ibkr_flex.IBKRFlexConnector.parse_statement(load_fixture("ibkr_send_ok.xml"))
    assert err.value.code == "bad_response"
    with pytest.raises(BrokerError) as err:
        ibkr_flex.IBKRFlexConnector.parse_statement("not xml at all")
    assert err.value.code == "bad_response"


def test_xml_parser_uses_stdlib_elementtree_only():
    assert ibkr_flex.ET is ET  # stdlib ElementTree, no external entity processing


def test_capabilities():
    connector, _, _ = make_connector()
    assert connector.capabilities() == {
        "accounts": False,
        "positions": False,
        "trades": True,
        "income": True,
        "corporate_actions": True,
        "history": True,
        "quotes": False,
    }
