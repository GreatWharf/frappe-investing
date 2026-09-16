"""Offline orchestration tests: fake Frappe store, real engine/services logic."""

import sys
from datetime import date
from decimal import Decimal as D
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
        self.store = store
        store[self.name] = self
        return self

    def submit(self):
        self.docstatus = 1
        store[self.name] = self
        from frappe_investing import services

        if self.doctype == "Investment Event":
            services.apply_event(self)
        return self

    def check_permission(self, *a):
        return None

    def get_password(self, field, **kw):
        return self.get(field)

    def _compute_dedupe(self):
        import hashlib

        if self.get("source") == "Manual" and not self.get("source_ref"):
            self.dedupe_key = None
            return
        raw = "|".join(str(x or "") for x in (self.account, self.source, self.source_ref))
        self.dedupe_key = hashlib.sha256(raw.encode()).hexdigest()[:40]

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
            source=self.get("source", "Manual"),
            source_ref=self.get("source_ref", ""),
        )


store = {}


@pytest.fixture
def services(monkeypatch):
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

    def get_doc(kind, name=None, **kw):
        if isinstance(kind, dict):
            doc = Row(kind)
            doc.store = store
            return doc
        return store[name]

    def get_value(dt, filters, fieldname=None, **kw):
        rows = _match(dt, filters)
        if not rows:
            return None
        if isinstance(fieldname, str):
            return rows[0].get(fieldname)
        return rows[0]

    def get_all(dt, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None, **kw):
        rows = _match(dt, filters)
        if pluck:
            values = [r.get(pluck) for r in rows]
            if kw.get("distinct"):
                values = list(dict.fromkeys(v for v in values if v is not None))
            return values
        return rows

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
        if op == "<=":
            return value is not None and value <= target
        if op == "between":
            return value is not None and target[0] <= value <= target[1]
        return value == target

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
    utils = ModuleType("frappe.utils")
    utils.today = lambda: "2026-09-16"
    utils.now_datetime = lambda: "2026-09-16 12:00:00"
    utils.add_days = lambda d, n: d
    utils.cint = int
    monkeypatch.setitem(sys.modules, "frappe", fake)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    for name in ("frappe_investing.services", "frappe_investing.license_service"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import frappe_investing.services as services_mod

    yield services_mod, fake, store
    sys.modules.pop("frappe_investing.services", None)
    sys.modules.pop("frappe_investing.license_service", None)


def _event(store, **kw):
    doc = Row({"doctype": "Investment Event", **kw})
    doc.store = store
    doc.insert()
    doc._compute_dedupe()
    return doc


def test_record_event_is_idempotent_by_source_ref(services):
    services_mod, fake, store = services
    data = dict(
        event_type="Buy",
        posting_date=date(2026, 1, 5),
        account="a1",
        security="sec-aapl",
        qty="10",
        price="10",
        currency="USD",
        source="Zerodha",
        source_ref="trade-1",
    )
    first, created1 = services_mod.record_event(dict(data))
    second, created2 = services_mod.record_event(dict(data))
    assert created1 is True and created2 is False and first == second
    assert len([r for r in store.values() if r.get("doctype") == "Investment Event"]) == 1


def test_apply_event_creates_lot_and_allocation_on_sell(services):
    services_mod, fake, store = services
    buy = _event(
        store,
        event_type="Buy",
        posting_date=date(2026, 1, 5),
        account="a1",
        security="sec-aapl",
        qty="100",
        price="10",
        currency="USD",
        source="Manual",
    )
    services_mod.apply_event(buy)
    lots = [r for r in store.values() if r.get("doctype") == "Tax Lot"]
    assert len(lots) == 1 and D(lots[0].qty_open) == D(100)
    sell = _event(
        store,
        event_type="Sell",
        posting_date=date(2026, 2, 1),
        account="a1",
        security="sec-aapl",
        qty="40",
        price="15",
        currency="USD",
        source="Manual",
    )
    services_mod.apply_event(sell)
    allocs = [r for r in store.values() if r.get("doctype") == "Lot Allocation"]
    assert len(allocs) == 1
    assert D(allocs[0].realized_pnl) == D(200)
    assert D(lots[0].qty_open) == D(60)


def test_value_portfolio_aggregates_with_prices(services):
    services_mod, fake, store = services
    services_mod.apply_event(
        _event(
            store,
            event_type="Buy",
            posting_date=date(2026, 1, 5),
            account="a1",
            security="sec-aapl",
            qty="100",
            price="10",
            currency="USD",
            source="Manual",
        )
    )
    store["sp1"] = Row(
        doctype="Security Price",
        name="sp1",
        security="sec-aapl",
        date=date(2026, 2, 1),
        close="12.50",
        currency="USD",
        source="Manual",
    )
    result = services_mod.value_portfolio("p1", day=date(2026, 2, 1))
    assert result["total_value"] == D("1250.00")
    assert result["unrealized_pnl"] == D("250.00")
    assert result["stale"] == []


def test_snapshot_is_idempotent_per_day(services):
    services_mod, fake, store = services
    services_mod.apply_event(
        _event(
            store,
            event_type="Buy",
            posting_date=date(2026, 1, 5),
            account="a1",
            security="sec-aapl",
            qty="10",
            price="10",
            currency="USD",
            source="Manual",
        )
    )
    first = services_mod.snapshot_portfolio("p1", day=date(2026, 2, 1))
    second = services_mod.snapshot_portfolio("p1", day=date(2026, 2, 1))
    assert first == second
    assert len([r for r in store.values() if r.get("doctype") == "Portfolio Snapshot"]) == 1


def test_performance_summary_reports_ytd_realized_and_income(services):
    services_mod, fake, store = services
    buy = _event(
        store,
        event_type="Buy",
        posting_date=date(2026, 1, 5),
        account="a1",
        security="sec-aapl",
        qty="100",
        price="10",
        currency="USD",
        source="Manual",
        docstatus=1,
    )
    services_mod.apply_event(buy)
    sell = _event(
        store,
        event_type="Sell",
        posting_date=date(2026, 2, 1),
        account="a1",
        security="sec-aapl",
        qty="40",
        price="15",
        currency="USD",
        source="Manual",
        docstatus=1,
    )
    services_mod.apply_event(sell)
    _event(
        store,
        event_type="Dividend",
        posting_date=date(2026, 1, 20),
        account="a1",
        security="sec-aapl",
        gross="30",
        taxes="3",
        currency="USD",
        source="Manual",
        docstatus=1,
    )
    summary = services_mod.performance_summary("p1", as_of=date(2026, 2, 1))
    assert summary["realized_pnl_ytd"] == D(200)  # 40 × (15 − 10)
    assert summary["income_ytd"] == D(27)  # 30 gross − 3 withholding
    assert summary["twr_ytd"] == D(0)  # no snapshots yet
    assert summary["xirr_ytd"] is None  # no external flows


def test_sync_event_payload_carries_connection(services, monkeypatch):
    services_mod, fake, store = services
    store["conn-1"] = Row(
        doctype="Broker Connection",
        name="conn-1",
        connection_name="Kite Main",
        broker="Zerodha",
        company="Acme",
        enabled=1,
    )
    store["a1"].broker_connection = "conn-1"
    import frappe_investing.sync_service as sync_mod

    doc = sync_mod._event_to_doc(
        store["conn-1"],
        {
            "type": "Buy",
            "date": date(2026, 1, 5),
            "security_key": "NSE:RELIANCE",
            "qty": "5",
            "price": "2500",
            "currency": "INR",
            "source_ref": "zt-1",
        },
    )
    assert doc["connection"] == "conn-1"
    assert doc["source"] == "Zerodha"
    assert doc["account"] == "a1"


def _license_state(**kw):
    from frappe_investing import licensing

    defaults = dict(
        status="active", tier="pro", customer="Acme", expires="2027-09-16",
        max_asset_classes=5, max_value=None, value_currency=None,
    )
    return licensing.LicenseState(**{**defaults, **kw})


def _license_mod(services, state):
    services_mod, fake, store = services
    import frappe_investing.license_service as license_mod

    store["Investment License"].license_key = "FINV1.fake.fake"
    license_mod.evaluate = lambda key, **kw: state
    return license_mod


def test_require_asset_class_free_tier_blocks_a_second_class(services):
    license_mod = _license_mod(
        services, _license_state(status="none", tier="standard", max_asset_classes=1)
    )
    license_mod.require_asset_class("Stock")  # already in use: allowed
    with pytest.raises(PermissionError, match="1 asset class"):
        license_mod.require_asset_class("Bond")


def test_require_asset_class_unlimited_never_blocks(services):
    license_mod = _license_mod(services, _license_state(max_asset_classes=None))
    license_mod.require_asset_class("Crypto")  # no exception


def test_public_state_exposes_limits(services):
    license_mod = _license_mod(
        services, _license_state(max_asset_classes=3, max_value="250000", value_currency="USD")
    )
    public = license_mod.public_state()
    assert public["max_asset_classes"] == 3
    assert public["max_value"] == "250000"
    assert public["value_currency"] == "USD"
    assert public["status"] == "active"


def test_portfolio_value_check_same_currency_breach(services):
    license_mod = _license_mod(
        services, _license_state(max_value="100000", value_currency="USD")
    )
    assert license_mod.portfolio_value_check(D("90000"), "USD") is None
    breach = license_mod.portfolio_value_check(D("120000"), "USD")
    assert breach["breached"] and breach["reason"] == "over_cap"
    assert breach["rate_used"] == "1"


def test_portfolio_value_check_converts_through_fx(services):
    license_mod = _license_mod(
        services, _license_state(max_value="100000", value_currency="USD")
    )
    services[2]["fx1"] = Row(
        doctype="FX Rate",
        name="fx1",
        from_currency="INR",
        to_currency="USD",
        date=date(2026, 9, 1),
        rate="0.012",
    )
    # ₹9,000,000 × 0.012 = $108,000 > $100,000 cap
    breach = license_mod.portfolio_value_check(D("9000000"), "INR")
    assert breach["reason"] == "over_cap"
    assert D(breach["converted_value"]) == D("108000.000000")


def test_portfolio_value_check_missing_rate_fails_closed(services):
    license_mod = _license_mod(
        services, _license_state(max_value="100000", value_currency="USD")
    )
    breach = license_mod.portfolio_value_check(D("10"), "EUR")
    assert breach["breached"] and breach["reason"] == "missing_fx"


def test_portfolio_value_check_no_cap_is_quiet(services):
    license_mod = _license_mod(services, _license_state())
    assert license_mod.portfolio_value_check(D("999999999"), "JPY") is None
