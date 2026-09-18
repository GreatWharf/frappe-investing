"""sync_connection recovery: non-BrokerError capture + commit-only-on-success.

Proves audit findings 2 and 3: any exception during a sync leaves log.error
and connection status/last_error set (never a blank Failed log with a
connection stuck on Connected), and a failed batch rolls back instead of
half-committing submitted events. All offline against the fake Frappe store
(same Row pattern as tests/test_api_import.py).
"""

import sys
from types import ModuleType

import pytest


class Row(dict):
    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        return self.get(key)

    __setattr__ = dict.__setitem__

    def insert(self, **kw):
        self.name = self.get("name") or f"{self.doctype}-{len(store)}"
        if self.get("doctype") == "Broker Sync Log" and not self.get("started_at"):
            pass
        store[self.name] = self
        return self

    def save(self, **kw):
        store[self.name] = self
        return self

    def check_permission(self, *a):
        return None


store = {}


@pytest.fixture
def sync(monkeypatch):
    store.clear()
    store["CONN-1"] = Row(
        doctype="Broker Connection",
        name="CONN-1",
        broker="Zerodha",
        enabled=1,
        status="Connected",
        last_error="",
        company="CO",
        api_key="k",
    )
    fake = ModuleType("frappe")
    fake.flags = Row()
    fake.local = ModuleType("frappelocal")
    fake.local.site = "test.local"
    fake.PermissionError = PermissionError
    fake.as_json = lambda v: __import__("json").dumps(v)

    committed, rolled_back = [], []

    class FakeDB:
        def __init__(self):
            # Faithful rollback: snapshot the store at each commit; rollback
            # restores it (like MariaDB discarding the open transaction).
            self.snapshots = [dict(store)]

        def set_value(self, dt, name, values, **kw):
            store[name].update(values)

        def commit(self):
            committed.append(1)
            self.snapshots.append(dict(store))

        def rollback(self):
            rolled_back.append(1)
            store.clear()
            store.update(self.snapshots[-1])

    fake.db = FakeDB()
    fake.get_doc = lambda kind, name=None, **kw: (
        Row(kind) if isinstance(kind, dict) else store[name]
    )

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
    mod._test_commits = (committed, rolled_back)
    return mod


def test_non_broker_error_sets_log_error_and_connection_status(sync):
    class ExplodingConnector:
        def capabilities(self):
            return {"positions": True}

        def positions(self):
            raise AttributeError("no such method")  # like the old Zerodha bug

    sync.connector_for = lambda connection: ExplodingConnector()
    with pytest.raises(AttributeError):
        sync.sync_connection("CONN-1")
    logs = [r for r in store.values() if r.get("doctype") == "Broker Sync Log"]
    assert len(logs) == 1
    assert logs[0]["status"] == "Failed"
    assert "no such method" in logs[0]["error"]
    assert store["CONN-1"]["status"] == "Error"
    assert "no such method" in store["CONN-1"]["last_error"]


def test_failed_batch_rolls_back_instead_of_half_committing(sync):
    committed, rolled_back = sync._test_commits

    class FakeConnector:
        def capabilities(self):
            return {"positions": False}

    sync.connector_for = lambda connection: FakeConnector()
    sync._connector_events = lambda *a: ([{"type": "Buy"}], False, None)
    sync._event_to_doc = lambda *a: {"event_type": "Buy"}

    def fail_record(doc):
        raise sync.BrokerError("broker blew up", code="bad_response")

    sync.record_event = fail_record
    with pytest.raises(sync.BrokerError):
        sync.sync_connection("CONN-1")
    assert rolled_back, "failure must roll back the partial batch"
    logs = [r for r in store.values() if r.get("doctype") == "Broker Sync Log"]
    assert len(logs) == 1 and logs[0]["status"] == "Failed"
    assert logs[0]["error"] == "broker blew up"


def test_success_still_commits(sync):
    committed, rolled_back = sync._test_commits

    class FakeConnector:
        def capabilities(self):
            return {"positions": False}

    sync.connector_for = lambda connection: FakeConnector()
    sync._connector_events = lambda *a: ([], False, None)
    sync.sync_connection("CONN-1")
    assert committed, "success must commit"
    assert not rolled_back
    logs = [r for r in store.values() if r.get("doctype") == "Broker Sync Log"]
    assert len(logs) == 1 and logs[0]["status"] == "Success"
