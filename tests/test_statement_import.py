"""Statement CSV importer: new-column parsing (withholding alias, FX legs,
bond accrual, specific-ID lots) plus the import preview/post orchestration.

Offline: the orchestration tests run against the same fake-Frappe store
pattern as tests/test_services.py, with services.record_event monkeypatched
to capture what would be posted — no Frappe runtime needed.
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from frappe_investing.connectors import csv_import


def _parse(text, **kw):
    return csv_import.parse_csv(text, **kw)


HEADER = (
    "date,type,security_key,qty,price,amount,gross,fees,taxes,withholding,currency,"
    "accrued_interest,split_ratio,basis_allocation,child_security,child_ratio,"
    "lot_ids,target_currency,target_amount,notes,source_ref"
)


def test_withholding_column_feeds_taxes():
    text = HEADER + "\n2026-02-01,Dividend,US:AAPL,,,,24.50,0,,3.68,USD,,,,,,,,,quarterly,DIV-1\n"
    result = _parse(text)
    assert result["errors"] == []
    (event,) = result["events"]
    assert event["taxes"] == "3.68"
    assert "withholding" not in event  # alias, never a separate passthrough


def test_taxes_and_withholding_together_are_rejected():
    text = (
        HEADER + "\n2026-02-01,Dividend,US:AAPL,,,,24.50,0,1.00,3.68,USD"
        ",,,,,,,,,both filled,DIV-2\n"
    )
    result = _parse(text)
    assert result["events"] == []
    assert len(result["errors"]) == 1
    assert "withholding" in result["errors"][0]["message"]


def test_new_columns_still_covered_by_decimal_guard():
    text = (
        HEADER + "\n2026-02-01,Dividend,US:AAPL,,,,24.50,0,,bogus,USD"
        ",,,,,,,,,bad withholding,DIV-3\n"
    )
    result = _parse(text)
    assert result["events"] == []
    assert "withholding" in result["errors"][0]["message"]


def test_fx_conversion_row_parses_with_target_leg():
    text = (
        HEADER + "\n2026-05-01,FX Conversion,,,,1000,,,0,,USD"
        ",,,,,,,EUR,920,converted,FX-1\n"
    )
    result = _parse(text)
    assert result["errors"] == []
    (event,) = result["events"]
    assert event["type"] == "FX Conversion"
    assert event["amount"] == "1000"
    assert event["target_currency"] == "EUR"
    assert event["target_amount"] == "920"


def test_fx_conversion_without_target_leg_is_rejected_with_line_number():
    text = HEADER + "\n2026-05-01,FX Conversion,,,,1000,,,0,,USD,,,,,,,,no target,FX-2\n"
    result = _parse(text)
    assert result["events"] == []
    assert result["errors"][0]["row"] == 2
    assert "target" in result["errors"][0]["message"].lower()


def test_accrued_interest_parses_through():
    text = (
        HEADER + "\n2026-01-15,Buy,US:BOND1,10,98.5,,,0.5,0,,USD"
        ",12.25,,,,,,,,first bond buy,BND-1\n"
    )
    result = _parse(text)
    assert result["errors"] == []
    (event,) = result["events"]
    assert event["accrued_interest"] == "12.25"


def test_lot_ids_parses_to_specific_identification_list():
    text = (
        HEADER + "\n2026-02-01,Sell,US:AAPL,5,200,,,,0,,USD"
        ",,,,,,LOT-1; LOT-2,,,spec sell,SPEC-1\n"
    )
    result = _parse(text)
    assert result["errors"] == []
    (event,) = result["events"]
    assert event["lot_ids"] == "LOT-1; LOT-2"


# ---------------------------------------------------------------- preview/post
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
        return self

    def submit(self):
        self.docstatus = 1
        return self

    def check_permission(self, *a):
        return None

    def _compute_dedupe(self):
        import hashlib

        if self.get("source") == "Manual" and not self.get("source_ref"):
            return
        raw = "|".join(str(x or "") for x in (self.get("account"), self.get("source"), self.get("source_ref")))
        self.dedupe_key = hashlib.sha256(raw.encode()).hexdigest()[:40]


store = {}


@pytest.fixture
def importer(monkeypatch):
    store.clear()
    store["p1"] = Row(
        doctype="Portfolio", name="p1", portfolio_name="Main", company="Acme", base_currency="USD"
    )
    store["a1"] = Row(
        doctype="Investment Account",
        name="a1",
        account_name="Main",
        portfolio="p1",
        currency="USD",
        enabled=1,
    )
    store["a2"] = Row(
        doctype="Investment Account",
        name="a2",
        account_name="Other",
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
            doc = Row(kind)
            return doc
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
    utils.now_datetime = lambda: "2026-09-16 12:00:00"
    utils.flt = float
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    for name in ("frappe_investing.services", "frappe_investing.importer", "frappe_investing.api"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    posted = []

    import frappe_investing.services as services_mod

    def fake_record_event(data, *, submit=True):
        posted.append(dict(data))
        return f"EV-{len(posted):04d}", True

    monkeypatch.setattr(services_mod, "record_event", fake_record_event)
    import frappe_investing.importer as importer_mod

    yield importer_mod, fake, store, posted
    for name in ("frappe_investing.services", "frappe_investing.importer", "frappe_investing.api"):
        sys.modules.pop(name, None)


GOOD_CSV = (
    "date,type,security_key,qty,price,amount,gross,fees,taxes,currency,notes,source_ref\n"
    "2026-01-15,Buy,US:AAPL,10,190.25,,,1.00,0,USD,first buy,ORD-1\n"
    "2026-02-01,Dividend,US:AAPL,,,,24.50,0,3.68,USD,quarterly div,DIV-1\n"
)

BAD_CSV = (
    "date,type,security_key,qty,price,amount,gross,fees,taxes,currency,notes,source_ref\n"
    "2026-01-15,Buy,US:AAPL,10,190.25,,,1.00,0,USD,good row,BAD-OK\n"
    "2026-13-40,Buy,US:AAPL,10,190.25,,,1.00,0,USD,bad date,BAD-DATE\n"
)


def test_preview_counts_and_never_posts(importer):
    importer_mod, fake, store, posted = importer
    preview = importer_mod.preview_import(GOOD_CSV, account="a1")
    assert preview["total_rows"] == 2
    assert preview["valid_rows"] == 2
    assert preview["error_rows"] == 0
    assert preview["errors"] == []
    assert preview["by_type"] == {"Buy": 1, "Dividend": 1}
    assert posted == []  # preview never writes
    dividend = next(e for e in preview["events"] if e["type"] == "Dividend")
    assert dividend["taxes"] == "3.68"


def test_preview_reports_per_row_errors(importer):
    importer_mod, fake, store, posted = importer
    preview = importer_mod.preview_import(BAD_CSV, account="a1")
    assert preview["total_rows"] == 2
    assert preview["valid_rows"] == 1
    assert preview["error_rows"] == 1
    assert preview["errors"][0]["row"] == 3
    assert posted == []


def test_post_refuses_with_errors(importer):
    importer_mod, fake, store, posted = importer
    with pytest.raises(ValueError, match="[Ff]ix") as exc_info:
        importer_mod.post_import(BAD_CSV, account="a1")
    assert exc_info.value.errors[0]["row"] == 3  # per-row errors ride along
    assert posted == []


def test_post_unknown_security_is_a_row_error_not_a_silent_create(importer):
    importer_mod, fake, store, posted = importer
    text = (
        "date,type,security_key,qty,price,currency,source_ref\n"
        "2026-01-15,Buy,US:UNKNOWN,10,5,USD,UNK-1\n"
    )
    with pytest.raises(ValueError, match="[Uu]nknown security") as exc_info:
        importer_mod.post_import(text, account="a1")
    assert exc_info.value.errors[0]["row"] == 2
    assert exc_info.value.errors[0]["message"].startswith("Unknown security")
    assert posted == []
    assert not [r for r in store.values() if r.get("doctype") == "Security" and r.get("ticker") == "US:UNKNOWN"]


def test_post_routes_every_row_through_record_event(importer):
    importer_mod, fake, store, posted = importer
    result = importer_mod.post_import(GOOD_CSV, account="a1")
    assert result["posted"] == 2
    assert result["created"] == 2
    assert result["errors"] == []
    assert [p["event_type"] for p in posted] == ["Buy", "Dividend"]
    assert all(p["account"] == "a1" for p in posted)
    assert all(p["source"] == "CSV Import" for p in posted)
    buy = posted[0]
    assert buy["security"] == "sec-aapl"  # ticker resolved to the Security name
    assert buy["source_ref"] == "ORD-1"
    dividend = posted[1]
    assert dividend["taxes"] == "3.68"  # withholding wired through to accounting
    assert dividend["gross"] == "24.50"


def test_post_unknown_account_is_rejected(importer):
    importer_mod, fake, store, posted = importer
    with pytest.raises(ValueError, match="[Aa]ccount"):
        importer_mod.post_import(GOOD_CSV, account="nope")
    assert posted == []


def test_post_resolves_ticker_by_isin_like_sync(importer):
    importer_mod, fake, store, posted = importer
    store["sec-foo"] = Row(
        doctype="Security",
        name="sec-foo",
        security_name="Foo",
        asset_class="Stock",
        currency="USD",
        ticker="OTHER",
        isin="US123",
        status="Active",
    )
    text = (
        "date,type,security_key,qty,price,currency,source_ref\n"
        "2026-01-15,Buy,ISIN:US123,10,5,USD,ISIN-1\n"
    )
    result = importer_mod.post_import(text, account="a1")
    assert result["posted"] == 1
    assert posted[0]["security"] == "sec-foo"


def test_post_is_idempotent_on_reimport(importer):
    importer_mod, fake, store, posted = importer
    import frappe_investing.services as services_mod

    seen = {}

    def dedupe_record_event(data, *, submit=True):
        key = (data.get("account"), data.get("source"), data.get("source_ref"))
        if key in seen:
            return seen[key], False
        seen[key] = f"EV-{len(seen) + 1:04d}"
        posted.append(dict(data))
        return seen[key], True

    services_mod.record_event = dedupe_record_event
    first = importer_mod.post_import(GOOD_CSV, account="a1")
    second = importer_mod.post_import(GOOD_CSV, account="a1")
    assert first["posted"] == 2 and first["created"] == 2
    assert second["posted"] == 2 and second["created"] == 0  # dedupe, like manual events


def test_cost_method_surface_reports_portfolio_and_default(importer):
    importer_mod, fake, store, posted = importer
    store["Investment Settings"] = Row(
        doctype="Investment Settings",
        name="Investment Settings",
        default_cost_method="AVERAGE",
    )
    info = importer_mod.cost_method_info("a1")
    assert info == {"portfolio": "p1", "portfolio_cost_method": None, "effective": "AVERAGE"}
    store["p1"]["cost_method"] = "SPECIFIC"
    info = importer_mod.cost_method_info("a1")
    assert info["effective"] == "SPECIFIC"


def test_cost_method_surface_unknown_account_is_rejected(importer):
    importer_mod, fake, store, posted = importer
    with pytest.raises(ValueError, match="[Aa]ccount"):
        importer_mod.cost_method_info("nope")
