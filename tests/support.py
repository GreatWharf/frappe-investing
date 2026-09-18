"""Shared offline harness: fake-Frappe store plus real engine/services logic.

One home for the Row/store/fake-frappe pattern currently duplicated across
tests/test_services.py and tests/test_statement_import.py, so new offline
tests (reversal, accounting wiring, role gates, tier gates) reuse one fixture
instead of copying it. No Frappe runtime needed.
"""

import json
import sys
from datetime import datetime
from decimal import Decimal as D
from types import ModuleType, SimpleNamespace

import pytest

NOW = datetime(2026, 9, 16, 12, 0, 0)


class Row(dict):
    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        return self.get(key)

    __setattr__ = dict.__setitem__

    def update(self, values):
        for key, value in dict(values).items():
            self[key] = value
        return self

    def set(self, key, value):
        self[key] = value

    def save(self, **kw):
        store[self.name] = self
        return self

    def insert(self, **kw):
        self.name = self.get("name") or f"{self.doctype}-{len(store)}"
        store[self.name] = self
        self.store = store
        return self

    def submit(self):
        self.docstatus = 1
        store[self.name] = self
        from frappe_investing import services

        if self.doctype == "Investment Event":
            services.apply_event(self)
        return self

    def cancel(self):
        # dict.__setattr__ stores into the mapping, so a per-row override
        # (je_row.cancel = ...) lands in self["cancel"]; honor it so tests
        # can observe JE cancellation.
        wrapped = self.get("cancel")
        if callable(wrapped):
            return wrapped()
        self.docstatus = 2
        store[self.name] = self
        return self

    def check_permission(self, *a):
        return None

    def get_password(self, field, **kw):
        return self.get(field)

    def delete(self, **kw):
        store.pop(self.name, None)

    def _compute_dedupe(self):
        # Delegates to the REAL production implementation so the harness can
        # never drift from it (audit: fixture reimplemented to_core_event and
        # drifted). Row attribute access mirrors Document behavior closely
        # enough for the pure-hash computation.
        from frappe_investing.documents import ManagedDocument

        return ManagedDocument._compute_dedupe(self)

    def to_core_event(self):
        from frappe_investing.core.events import Event
        from frappe_investing.core.money import dec

        def d(f):
            v = self.get(f)
            return None if v in (None, "") else dec(str(v))

        return Event(
            type=self.event_type,
            date=self.posting_date,
            account=self.account,
            currency=self.currency,
            security=self.security,
            qty=d("qty"),
            price=d("price"),
            amount=d("amount"),
            gross=d("gross"),
            fees=d("fees") or D(0),
            taxes=d("taxes") or D(0),
            accrued_interest=d("accrued_interest") or D(0),
            split_ratio=d("split_ratio"),
            basis_allocation=d("basis_allocation"),
            child_ratio=d("child_ratio"),
            child_security=self.get("child_security"),
            target_currency=self.get("target_currency"),
            target_amount=d("target_amount"),
            lot_ids=tuple(x.strip() for x in (self.get("lot_ids") or "").split(",") if x.strip()),
            source=self.get("source", "Manual"),
            source_ref=self.get("source_ref", ""),
            notes=self.get("notes") or "",
            meta=json.loads(self.get("meta_json") or "{}"),
        )


store = {}


def seed(store):
    store.clear()
    store["Investment Settings"] = Row(
        doctype="Investment Settings",
        name="Investment Settings",
        company="Acme",
        default_cost_method="FIFO",
        auto_accounting="Off",
        price_provider="Manual",
        broker_sync_enabled=1,
    )
    store["Acme"] = Row(doctype="Company", name="Acme", default_currency="USD")
    store["Investment License"] = Row(
        doctype="Investment License",
        name="Investment License",
        license_key="",
        tier="standard",
        status="none",
        customer="",
        expires=None,
        validated_at=None,
        status_note="",
        limits="",
    )
    store["p1"] = Row(
        doctype="Portfolio",
        name="p1",
        portfolio_name="Main",
        company="Acme",
        base_currency="USD",
        cost_method=None,
    )
    store["a1"] = Row(
        doctype="Investment Account",
        name="a1",
        account_name="IB Main",
        portfolio="p1",
        currency="USD",
        enabled=1,
    )
    store["sec-aapl"] = Row(
        doctype="Security",
        name="sec-aapl",
        security_name="Apple",
        asset_class="Stock",
        currency="USD",
        ticker="AAPL",
        status="Active",
    )


def fake_frappe(monkeypatch, *, roles=("Investment Manager", "System Manager"), only_for=None):
    """Stub the frappe module the way the offline harness does.

    only_for defaults to enforcing Investment roles (raising PermissionError
    when the caller's roles lack them), so role-gate tests exercise the real
    enforcement instead of a no-op stub. Pass only_for=lambda *a: None for
    the legacy no-op behavior.
    """
    calls = {"only_for": []}
    current = {"roles": list(roles)}

    fake = ModuleType("frappe")
    fake.session = SimpleNamespace(user="manager@example.test")
    fake.flags = Row()
    fake.PermissionError = PermissionError
    fake.ValidationError = ValueError
    fake.throw = lambda msg, exc=ValueError, **kw: (_ for _ in ()).throw(exc(msg))
    if only_for is not None:
        fake.only_for = only_for
    else:

        def only_for(*allowed):
            calls["only_for"].append(allowed)
            group = allowed[0] if allowed and isinstance(allowed[0], (list, tuple)) else allowed
            if not set(group).intersection(current["roles"]):
                raise PermissionError(f"Not permitted. Requires one of: {', '.join(group)}")

        fake.only_for = only_for
    fake.get_roles = lambda *a: list(current["roles"])
    fake.local = SimpleNamespace(site="test.local")
    fake.log_error = lambda **kw: None
    fake.as_json = lambda v: __import__("json").dumps(v)
    fake._dict = Row
    fake.whitelist = lambda *a, **kw: (lambda fn: fn)
    fake.parse_json = lambda v: __import__("json").loads(v) if isinstance(v, str) else v
    fake.enqueue = lambda *a, **kw: {"queued": True}

    def get_doc(kind, name=None, **kw):
        if isinstance(kind, dict):
            doc = Row(kind)
            doc.store = store
            return doc
        doc = store[name]
        doc.store = store
        return doc

    def _match(dt, filters):
        if isinstance(filters, str):
            return [store[filters]] if filters in store and store[filters].get("doctype") == dt else []
        result = [r for r in store.values() if r.get("doctype") == dt]
        for key, cond in (filters or {}).items():
            if isinstance(cond, (list, tuple)) and len(cond) == 2:
                op, val = cond
                result = [r for r in result if _op(r.get(key), op, val)]
            else:
                result = [r for r in result if r.get(key) == cond]
        return result

    def _op(value, op, target):
        if op == "in":
            return value in target
        if op == ">":
            return value is not None and value > target
        if op == ">=":
            return value is not None and value >= target
        if op == "<=":
            return value is not None and value <= target
        if op == "!=":
            return value != target
        if op == "between":
            return value is not None and target[0] <= value <= target[1]
        return value == target

    def get_value(dt, filters, fieldname=None, **kw):
        rows = _match(dt, filters)
        if not rows:
            return None
        if isinstance(fieldname, str):
            return rows[0].get(fieldname)
        if isinstance(fieldname, (list, tuple)):
            # Production returns an object with attribute access when given a
            # list of fields (as_dict=True); mirror that, not a plain list.
            return Row({f: rows[0].get(f) for f in fieldname})
        return rows[0]

    def get_all(dt, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None, **kw):
        rows = _match(dt, filters)
        if order_by:
            key, _, direction = order_by.partition(" ")
            rows = sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)), reverse=direction == "desc")
        if limit_page_length:
            rows = rows[:limit_page_length]
        if pluck:
            values = [r.get(pluck) for r in rows]
            if kw.get("distinct"):
                values = list(dict.fromkeys(v for v in values if v is not None))
            return values
        return rows

    fake.get_doc, fake.get_all = get_doc, get_all
    fake.get_value = get_value
    fake.get_single = lambda name: store[name]
    fake.get_cached_value = lambda dt, name, f: store[name].get(f)

    def set_value(dt, name, key, value=None, **kw):
        values = key if isinstance(key, dict) else {key: value}
        store[name].update(values)

    fake.db = SimpleNamespace(
        get_value=get_value,
        get_all=get_all,
        exists=lambda dt, f=None: bool(_match(dt, f)) if f else False,
        set_value=set_value,
        count=lambda dt, f=None: len(_match(dt, f)),
        escape=repr,
    )
    fake.delete_doc = lambda dt, name, **kw: store.pop(name, None)

    class Cache:
        def lock(self, name, **kw):
            class Lock:
                def acquire(self, blocking=False):
                    return True

                def release(self):
                    return None

            return Lock()

    fake.cache = Cache()
    fake.conf = {}
    model = ModuleType("frappe.model")
    model.__path__ = []
    document_mod = ModuleType("frappe.model.document")

    class Document(Row):
        def is_new(self):
            return not self.get("name") or self.get("__islocal", False)

        def get(self, key, default=None):
            return super().get(key, default)

    document_mod.Document = Document
    utils = ModuleType("frappe.utils")
    password_mod = ModuleType("frappe.utils.password")
    password_mod.set_encrypted_password = lambda *a, **kw: None
    dashboard_mod = ModuleType("frappe.utils.dashboard")
    dashboard_mod.cache_source = lambda fn=None, **kw: fn if fn else (lambda f: f)
    utils.today = lambda: "2026-09-16"
    utils.now_datetime = lambda: NOW
    utils.get_datetime = lambda v: v if hasattr(v, "year") else __import__("datetime").datetime.fromisoformat(str(v))
    utils.add_days = lambda d, n: d
    utils.cint = int
    utils.flt = float
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.model", model)
    monkeypatch.setitem(sys.modules, "frappe.model.document", document_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    monkeypatch.setitem(sys.modules, "frappe.utils.password", password_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils.dashboard", dashboard_mod)
    for name in (
        "frappe_investing.services",
        "frappe_investing.license_service",
        "frappe_investing.api",
        "frappe_investing.documents",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
    fake.calls = calls
    fake.current_roles = current
    return fake


def evict_app_modules():
    """Drop imported frappe_investing modules so the next test re-imports
    against its own stubbed frappe (modules bind `frappe` at import time)."""
    for name in list(sys.modules):
        if name == "frappe_investing" or name.startswith("frappe_investing."):
            sys.modules.pop(name, None)


def make_services_fixture(caller):
    """A pytest fixture yielding (services_mod, fake, store), seeded and stubbed."""

    @pytest.fixture
    def services(monkeypatch):
        seed(store)
        fake = fake_frappe(monkeypatch)
        import frappe_investing.services as services_mod

        yield services_mod, fake, store
        evict_app_modules()

    services.__module__ = caller
    return services


def event_doc(store, **kw):
    base = dict(
        doctype="Investment Event",
        event_type="Buy",
        posting_date=__import__("datetime").date(2026, 1, 5),
        account="a1",
        security="sec-aapl",
        qty="100",
        price="10",
        currency="USD",
        source="Manual",
    )
    base.update(kw)
    doc = Row(base)
    doc.store = store
    doc.insert()
    doc._compute_dedupe()
    return doc
