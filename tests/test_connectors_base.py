"""Shared connector plumbing: sanitized errors, size caps, session hygiene."""

from decimal import Decimal

import pytest
import requests

from frappe_investing.connectors.base import (
    MAX_RESPONSE_BYTES,
    BrokerError,
    ConnectorBase,
    http_json,
    http_request,
    new_session,
)
from tests.fixtures.fakes import FakeResponse, FakeSession

SECRET = "supersecretapikey123"


def test_new_session_disables_env_trust():
    # trust_env=False: no proxy env vars, no ~/.netrc attaching credentials implicitly
    assert new_session().trust_env is False


def test_base_capabilities_default_all_false():
    caps = ConnectorBase().capabilities()
    assert set(caps) == {
        "accounts",
        "positions",
        "trades",
        "income",
        "corporate_actions",
        "history",
        "quotes",
    }
    assert not any(caps.values())


def test_request_enforces_timeout_and_tls_verify():
    sess = FakeSession()
    sess.add("GET", "example.com", FakeResponse(200, b"{}"))
    http_request(sess, "GET", "https://example.com/x?token=abc")
    call = sess.calls[0]
    assert call["kwargs"]["timeout"] == 30
    assert call["kwargs"]["verify"] is True


def test_429_maps_to_rate_limited():
    sess = FakeSession()
    sess.add("GET", "api.kite.trade", FakeResponse(429, b"too many requests"))
    with pytest.raises(BrokerError) as err:
        http_request(sess, "GET", "https://api.kite.trade/portfolio/holdings")
    assert err.value.code == "rate_limited"


def test_errors_never_echo_secrets_query_strings_or_bodies():
    sess = FakeSession()
    body = f'{{"message": "Invalid api credentials for {SECRET}"}}'.encode()
    sess.add("GET", "api.kite.trade", FakeResponse(403, body))
    with pytest.raises(BrokerError) as err:
        http_request(sess, "GET", f"https://api.kite.trade/session?api_key={SECRET}")
    message = str(err.value)
    assert SECRET not in message
    assert "Invalid api credentials" not in message  # response body is never echoed
    assert "api.kite.trade" in message  # but host + status are, for debuggability
    assert "403" in message
    assert err.value.code == "auth"


def test_response_size_cap():
    sess = FakeSession()
    sess.add("GET", "big.example.com", FakeResponse(200, b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(BrokerError) as err:
        http_request(sess, "GET", "https://big.example.com/data")
    assert err.value.code == "too_large"


def test_content_length_header_short_circuits_large_responses():
    sess = FakeSession()
    sess.add(
        "GET",
        "big.example.com",
        FakeResponse(200, b"small", headers={"Content-Length": str(MAX_RESPONSE_BYTES + 50)}),
    )
    with pytest.raises(BrokerError) as err:
        http_request(sess, "GET", "https://big.example.com/data")
    assert err.value.code == "too_large"


def test_timeout_and_connection_errors_are_sanitized():
    sess = FakeSession()
    sess.raise_next(requests.Timeout(f"timed out reading https://h/x?api_key={SECRET}"))
    with pytest.raises(BrokerError) as err:
        http_request(sess, "GET", "https://api.kite.trade/x")
    assert err.value.code == "timeout"
    assert SECRET not in str(err.value)

    sess.raise_next(requests.ConnectionError(f"pool exhausted for https://h/x?api_key={SECRET}"))
    with pytest.raises(BrokerError) as err:
        http_request(sess, "GET", "https://api.kite.trade/x")
    assert err.value.code == "network_error"
    assert SECRET not in str(err.value)


def test_5xx_maps_to_server_error():
    sess = FakeSession()
    sess.add("GET", "api.kite.trade", FakeResponse(502, b"bad gateway"))
    with pytest.raises(BrokerError) as err:
        http_request(sess, "GET", "https://api.kite.trade/x")
    assert err.value.code == "server_error"


def test_http_json_parses_floats_as_decimal_not_binary_float():
    sess = FakeSession()
    sess.add("GET", "example.com", FakeResponse(200, '{"price": 191.70, "qty": 10}'))
    payload = http_json(sess, "GET", "https://example.com/q")
    assert payload["price"] == Decimal("191.70")
    assert isinstance(payload["price"], Decimal)
    assert str(payload["price"]) == "191.70"  # trailing zero preserved, no float noise


def test_http_json_invalid_body_is_bad_response_without_echoing_it():
    sess = FakeSession()
    sess.add("GET", "example.com", FakeResponse(200, f"not json but mentions {SECRET}"))
    with pytest.raises(BrokerError) as err:
        http_json(sess, "GET", "https://example.com/q")
    assert err.value.code == "bad_response"
    assert SECRET not in str(err.value)
