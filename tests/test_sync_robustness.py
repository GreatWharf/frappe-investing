"""Sync robustness: cursor advance, truncation, retry, honest queueing.

Proves audit findings 4, 5 and 7 plus the sync_now dedup finding: the cursor
and last_sync advance on complete runs (never on truncated ones), a capped
window marks the log Partial instead of Success, transient 429/timeouts are
retried with backoff, and sync_now reports queued False when dedup drops the
job. Offline: fake Frappe store + FakeSession HTTP routes, no network.
"""

import sys
from types import ModuleType

import pytest

from frappe_investing.connectors import base as conn_base
from frappe_investing.connectors import zerodha
from tests.fixtures.fakes import FakeResponse, FakeSession, load_fixture


class Row(dict):
    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        return self.get(key)

    __setattr__ = dict.__setitem__

    def insert(self, **kw):
        self.name = self.get("name") or f"{self.doctype}-{len(store)}"
        store[self.name] = self
        return self

    def save(self, **kw):
        store[self.name] = self
        return self

    def check_permission(self, *a):
        return None

    def get_password(self, field, **kw):
        return self.get(field)


store = {}


@pytest.fixture
def sync(monkeypatch):
    store.clear()
    store["CONN-A"] = Row(
        doctype="Broker Connection",
        name="CONN-A",
        broker="Alpaca",
        enabled=1,
        status="Connected",
        last_sync=None,
        last_error="",
        sync_cursor=None,
        company="CO",
        api_key="k",
    )
    fake = ModuleType("frappe")
    fake.flags = Row()
    fake.local = ModuleType("frappelocal")
    fake.local.site = "test.local"
    fake.PermissionError = PermissionError
    fake.as_json = lambda v: __import__("json").dumps(v)
    fake.log_error = lambda **kw: None

    class FakeDB:
        def __init__(self):
            self.values = {}

        def set_value(self, dt, name, key, value=None, **kw):
            if isinstance(key, dict):
                self.values.setdefault(name, {}).update(key)
                store[name].update(key)
            else:
                self.values.setdefault(name, {})[key] = value
                store[name][key] = value

        def get_value(self, *a, **kw):
            return None

        def exists(self, *a, **kw):
            return None

        def commit(self):
            pass

        def rollback(self):
            pass

    fake.db = FakeDB()
    fake.get_doc = lambda kind, name=None, **kw: (
        Row(kind) if isinstance(kind, dict) else store[name]
    )
    fake.get_all = lambda *a, **kw: []
    fake.get_cached_value = lambda *a, **kw: "USD"

    class Lock:
        def acquire(self, blocking=False):
            return True

        def release(self):
            return None

    class Cache:
        def lock(self, *a, **kw):
            return Lock()

    fake.cache = Cache()
    utils = ModuleType("frappe.utils")
    utils.now_datetime = lambda: "2026-09-16 12:00:00"
    utils.today = lambda: "2026-09-16"

    fake.utils = utils
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    for name in [
        "frappe_investing",
        "frappe_investing.sync_service",
        "frappe_investing.connectors",
        "frappe_investing.connectors.alpaca",
        "frappe_investing.connectors.ibkr_flex",
        "frappe_investing.connectors.zerodha",
        "frappe_investing.connectors.base",
        "frappe_investing.marketdata",
        "frappe_investing.marketdata.alphavantage",
        "frappe_investing.marketdata.coingecko",
        "frappe_investing.marketdata.stooq",
        "frappe_investing.marketdata.base",
        "frappe_investing.services",
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    pkg = ModuleType("frappe_investing")
    pkg.__path__ = ["frappe_investing"]
    monkeypatch.setitem(sys.modules, "frappe_investing", pkg)

    import importlib

    mod = importlib.import_module("frappe_investing.sync_service")
    mod.record_event = lambda doc: ("EV-1", True)
    return mod


def _alpaca(monkeypatch, sync, activities_body, positions_body="[]"):
    import importlib

    monkeypatch.delitem(sys.modules, "frappe_investing.connectors.alpaca", raising=False)
    alpaca = importlib.import_module("frappe_investing.connectors.alpaca")
    sess = FakeSession()
    sess.add("GET", "/v2/account/activities", FakeResponse(200, activities_body))
    sess.add("GET", "/v2/positions", FakeResponse(200, positions_body))
    conn = alpaca.AlpacaConnector("k", "s", session=sess)
    sync.connector_for = lambda connection: conn
    return sess


def test_complete_run_advances_cursor_and_last_sync(sync, monkeypatch):
    import json

    body = json.dumps(
        {
            "activities": [
                {"activity_type": "FILL", "id": "a1", "date": "2026-09-14",
                 "side": "buy", "symbol": "AAPL", "qty": "1", "price": "10",
                 "transaction_time": "2026-09-14T10:00:00Z"},
                {"activity_type": "FILL", "id": "a2", "date": "2026-09-15",
                 "side": "sell", "symbol": "AAPL", "qty": "1", "price": "11",
                 "transaction_time": "2026-09-15T10:00:00Z"},
            ]
        }
    )
    _alpaca(monkeypatch, sync, body)
    sync._event_to_doc = lambda *a: {"event_type": "Buy"}
    sync.sync_connection("CONN-A")
    assert store["CONN-A"]["sync_cursor"] == "2026-09-15"
    assert store["CONN-A"]["last_sync"] == "2026-09-16 12:00:00"
    assert store["CONN-A"]["status"] == "Connected"
    logs = [r for r in store.values() if r.get("doctype") == "Broker Sync Log"]
    assert len(logs) == 1 and logs[0]["status"] == "Success"


def test_truncated_run_marks_partial_and_keeps_cursor(sync, monkeypatch):
    import json

    body = json.dumps(
        {"activities": [
            {"activity_type": "FILL", "id": "a1", "date": "2026-09-15",
             "side": "buy", "symbol": "AAPL", "qty": "1", "price": "10",
             "transaction_time": "2026-09-15T10:00:00Z"}
        ]}
    )
    sess = _alpaca(monkeypatch, sync, body)
    # Force the cap on the *instance's* class: _alpaca re-imported the module,
    # so patch the same object the bound method reads globals from.
    import frappe_investing.connectors.alpaca as _fresh

    monkeypatch.setattr(_fresh, "MAX_PAGES", 0)  # cap hit immediately
    sync.connector_for = lambda connection: _fresh.AlpacaConnector("k", "s", session=sess)
    sync._event_to_doc = lambda *a: {"event_type": "Buy"}
    sync.sync_connection("CONN-A")
    logs = [r for r in store.values() if r.get("doctype") == "Broker Sync Log"]
    assert len(logs) == 1 and logs[0]["status"] == "Partial"
    assert "truncated" in logs[0]["error"]
    assert store["CONN-A"].get("sync_cursor") is None, "cursor must not advance on a capped run"
    assert store["CONN-A"]["last_sync"] == "2026-09-16 12:00:00"


def test_zerodha_cap_reports_truncated(monkeypatch):
    import json

    sess = FakeSession()
    orders = [{"order_id": str(1000 + i), "status": "COMPLETE", "transaction_type": "BUY",
               "exchange": "NSE", "tradingsymbol": "X", "quantity": 1,
               "average_price": 10, "order_timestamp": ""} for i in range(205)]
    sess.add("GET", "/orders", FakeResponse(200, json.dumps({"status": "success", "data": orders})))
    sess.add("GET", "/trades", FakeResponse(200, json.dumps({"status": "success", "data": []})))
    conn = zerodha.ZerodhaConnector("k", "s", access_token="t", session=sess)
    result = conn.todays_trades()
    assert result["truncated"] is True
    trade_calls = [c for c in sess.calls if c["url"].endswith("/trades")]
    assert len(trade_calls) == zerodha.MAX_TRADE_ORDER_FETCHES


def test_http_request_retries_rate_limit_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(conn_base, "_sleep_seconds", lambda attempt: sleeps.append(attempt))
    sess = FakeSession()
    sess.add("GET", "/x", FakeResponse(429, "slow down"))
    sess.push(FakeResponse(200, '{"ok": true}'))
    # route matches first, so pop the route after one hit via a fresh session pair:
    sess2 = FakeSession()
    sess2.push(FakeResponse(429, "slow down"))
    sess2.push(FakeResponse(200, '{"ok": true}'))
    body = conn_base.http_request(sess2, "GET", "https://api.example.com/x")
    assert body == b'{"ok": true}'
    assert sleeps == [1], "one backoff before the successful retry"


def test_http_request_gives_up_after_max_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(conn_base, "_sleep_seconds", lambda attempt: sleeps.append(attempt))
    sess = FakeSession()
    for _ in range(conn_base.MAX_RETRIES + 1):
        sess.push(FakeResponse(429, "slow down"))
    import pytest as _pytest

    with _pytest.raises(conn_base.BrokerError) as err:
        conn_base.http_request(sess, "GET", "https://api.example.com/x")
    assert err.value.code == "rate_limited"
    assert sleeps == [1, 2, 3]


def test_http_request_does_not_retry_auth_errors():
    sess = FakeSession()
    sess.push(FakeResponse(403, load_fixture("zerodha_error.json")))
    import pytest as _pytest

    with _pytest.raises(conn_base.BrokerError) as err:
        conn_base.http_request(sess, "GET", "https://api.kite.trade/portfolio/holdings")
    assert err.value.code == "auth"
    assert len(sess.calls) == 1, "non-transient failures fail fast"


def test_sync_now_reports_dedup_honestly(monkeypatch):
    from tests.support import fake_frappe

    fake = fake_frappe(monkeypatch)
    fake.enqueue = lambda *a, **kw: None  # dedup dropped the job
    import importlib

    api = importlib.import_module("frappe_investing.api")
    assert api.sync_now("CONN-1") == {
        "queued": False,
        "note": "A sync for this connection is already queued or running.",
    }
