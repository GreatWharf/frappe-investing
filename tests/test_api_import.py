"""Statement-import API tests: template URL, operations reference, cost-method
surface, preview persistence and post routing — all offline against the fake
Frappe store (same pattern as tests/test_services.py)."""

import sys
from types import ModuleType, SimpleNamespace

import pytest


class Row(dict):
    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        return self.get(key)

    __setattr__ = dict.__setitem__

    def set(self, key, value):
        self[key] = value

    def save(self, **kw):
        store[self.name] = self
        return self

    def insert(self, **kw):
        self.name = self.get("name") or f"{self.doctype}-{len(store)}"
        store[self.name] = self
        self["errors"] = [
            Row(doctype="Import Error", **c) if isinstance(c, dict) else c
            for c in (self.get("errors") or [])
        ]
        return self

    def submit(self):
        self.docstatus = 1
        return self

    def check_permission(self, *a):
        return None


store = {}


@pytest.fixture
def api(monkeypatch):
    store.clear()
    store["p1"] = Row(
        doctype="Portfolio",
        name="p1",
        portfolio_name="Main",
        company="Acme",
        base_currency="USD",
        cost_method="FIFO",
    )
    store["a1"] = Row(
        doctype="Investment Account",
        name="a1",
        account_name="Main",
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
        ticker="US:AAPL",
        status="Active",
    )
    store["Investment Settings"] = Row(
        doctype="Investment Settings",
        name="Investment Settings",
        default_cost_method="AVERAGE",
    )

    fake = ModuleType("frappe")
    fake.session = SimpleNamespace(user="manager@example.test")
    fake.flags = Row()
    fake.PermissionError = PermissionError
    fake.ValidationError = ValueError
    fake.throw = lambda msg, exc=ValueError, **kw: (_ for _ in ()).throw(exc(msg))
    fake.only_for = lambda *a: None
    fake.get_roles = lambda *a: ["Investment Manager", "System Manager"]
    fake.local = SimpleNamespace(site="test.local")
    fake.log_error = lambda **kw: None
    fake.as_json = lambda v: __import__("json").dumps(v)
    fake._dict = Row
    fake.whitelist = lambda *a, **kw: (lambda fn: fn)

    def get_doc(kind, name=None, **kw):
        if isinstance(kind, dict):
            return Row(kind)
        return store[name]

    def get_value(dt, filters, fieldname=None, **kw):
        rows = [r for r in store.values() if r.get("doctype") == dt]
        if isinstance(filters, str):
            rows = [store[filters]] if filters in store and store[filters].get("doctype") == dt else []
        else:
            for key, cond in (filters or {}).items():
                rows = [r for r in rows if r.get(key) == cond]
        if not rows:
            return None
        if isinstance(fieldname, str):
            return rows[0].get(fieldname)
        if isinstance(fieldname, (list, tuple)):
            return [rows[0].get(f) for f in fieldname]
        return rows[0]

    def get_all(dt, filters=None, fields=None, pluck=None, **kw):
        rows = [r for r in store.values() if r.get("doctype") == dt]
        if isinstance(filters, str):
            rows = [store[filters]] if filters in store and store[filters].get("doctype") == dt else []
        else:
            for key, cond in (filters or {}).items():
                rows = [r for r in rows if r.get(key) == cond]
        if pluck:
            return [r.get(pluck) for r in rows]
        return rows

    fake.get_doc, fake.get_all = get_doc, get_all
    fake.get_value = get_value
    fake.get_single = lambda name: store[name]

    def set_value(dt, name, key, value=None, **kw):
        values = key if isinstance(key, dict) else {key: value}
        store[name].update(values)

    fake.db = SimpleNamespace(get_value=get_value, get_all=get_all, set_value=set_value)
    utils = ModuleType("frappe.utils")
    utils.today = lambda: "2026-09-16"
    utils.flt = float
    utils.now_datetime = lambda: "2026-09-16 12:00:00"
    utils.get_datetime = lambda v: v
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    for name in (
        "frappe_investing",
        "frappe_investing.services",
        "frappe_investing.license_service",
        "frappe_investing.sync_service",
        "frappe_investing.importer",
        "frappe_investing.api",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    posted = []

    import frappe_investing.services as services_mod

    monkeypatch.setattr(
        services_mod, "record_event", lambda data, **kw: (posted.append(dict(data)), f"EV-{len(posted):04d}", True)[1:]
    )
    import frappe_investing.api as api_mod

    yield api_mod, posted
    for name in (
        "frappe_investing",
        "frappe_investing.services",
        "frappe_investing.license_service",
        "frappe_investing.sync_service",
        "frappe_investing.importer",
        "frappe_investing.api",
    ):
        sys.modules.pop(name, None)


GOOD_CSV = (
    "date,type,security_key,qty,price,amount,gross,fees,taxes,currency,notes,source_ref\n"
    "2026-01-15,Buy,US:AAPL,10,190.25,,,1.00,0,USD,first buy,ORD-1\n"
    "2026-02-01,Dividend,US:AAPL,,,,24.50,0,3.68,USD,quarterly div,DIV-1\n"
)


def test_import_reference_lists_every_supported_event_type(api):
    api_mod, posted = api
    ref = api_mod.import_reference()
    types = [row["type"] for row in ref["event_types"]]
    assert types == [
        "Buy", "Sell", "Dividend", "Coupon", "Interest", "Fee", "Deposit",
        "Withdrawal", "Transfer In", "Transfer Out", "Split", "Reverse Split",
        "Stock Dividend", "Spin-off", "Cash-in-lieu", "Redemption", "FX Conversion",
    ]
    by_type = {row["type"]: row for row in ref["event_types"]}
    assert by_type["Dividend"]["required"] == ["security_key", "gross"]
    assert "taxes / withholding" in by_type["Dividend"]["optional"]
    assert by_type["FX Conversion"]["required"] == ["amount", "target_currency", "target_amount"]
    assert by_type["Spin-off"]["required"] == [
        "security_key", "child_security", "basis_allocation (0–1)", "child_ratio"
    ]


def test_import_template_url_points_at_app_asset(api):
    api_mod, posted = api
    assert api_mod.import_template_url()["url"] == "/assets/frappe_investing/csv/statement_template.csv"


def test_import_cost_method_surfaces_portfolio_selector_backing(api):
    api_mod, posted = api
    assert api_mod.import_cost_method("a1") == {
        "portfolio": "p1",
        "portfolio_cost_method": "FIFO",
        "effective": "FIFO",
    }


def test_import_preview_counts_and_persists_errors_as_import_error_rows(api):
    api_mod, posted = api
    preview = api_mod.import_preview("a1", GOOD_CSV)
    assert preview["valid_rows"] == 2 and preview["error_rows"] == 0
    assert preview["by_type"] == {"Buy": 1, "Dividend": 1}
    assert posted == []
    batches = [r for r in store.values() if r.get("doctype") == "Import Batch"]
    assert len(batches) == 1
    assert batches[0]["status"] == "Draft"
    assert batches[0]["account"] == "a1"
    assert batches[0]["total_rows"] == 2 and batches[0]["imported"] == 2
    assert batches[0]["errors"] == []
    assert preview["batch"] == batches[0].name


def test_import_preview_with_bad_rows_persists_per_row_errors(api):
    api_mod, posted = api
    bad = GOOD_CSV + "2026-13-40,Buy,US:AAPL,10,190.25,,,1.00,0,USD,bad date,BAD-DATE\n"
    preview = api_mod.import_preview("a1", bad)
    assert preview["valid_rows"] == 2 and preview["error_rows"] == 1
    assert preview["errors"][0]["row"] == 4
    batches = [r for r in store.values() if r.get("doctype") == "Import Batch"]
    assert batches[0]["failed"] == 1
    assert batches[0]["errors"][0]["row_no"] == 4


def test_import_post_routes_through_record_event_with_dedupe_source(api):
    api_mod, posted = api
    result = api_mod.import_post("a1", GOOD_CSV)
    assert result["posted"] == 2 and result["created"] == 2 and result["errors"] == []
    assert [p["event_type"] for p in posted] == ["Buy", "Dividend"]
    assert all(p["source"] == "CSV Import" and p["account"] == "a1" for p in posted)
    assert posted[1]["taxes"] == "3.68"  # withholding wired through to accounting


def test_import_post_refuses_with_errors_and_posts_nothing(api):
    api_mod, posted = api
    bad = GOOD_CSV + "2026-13-40,Buy,US:AAPL,10,190.25,,,1.00,0,USD,bad date,BAD-DATE\n"
    with pytest.raises(ValueError, match="[Ff]ix"):
        api_mod.import_post("a1", bad)
    assert posted == []
