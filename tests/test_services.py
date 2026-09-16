"""Offline orchestration tests: fake Frappe store, real engine/services logic."""

import sys
from datetime import date, datetime, timedelta
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
    utils = ModuleType("frappe.utils")
    utils.today = lambda: "2026-09-16"
    utils.now_datetime = lambda: NOW
    utils.get_datetime = lambda v: v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
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


def test_benchmark_return_compares_price_return_in_base_currency(services):
    services_mod, fake, store = services
    # Nifty 50 is INR-denominated; portfolio base is USD, so the return is FX-converted.
    store["sec-bench-nifty"] = Row(
        doctype="Security",
        name="sec-bench-nifty",
        security_name="Nifty 50",
        ticker="BENCH:NIFTY50",
        asset_class="Benchmark",
        currency="INR",
        status="Active",
    )
    store["sp-b1"] = Row(
        doctype="Security Price",
        name="sp-b1",
        security="sec-bench-nifty",
        date=date(2026, 1, 2),
        close="22000",
        currency="INR",
        source="Stooq",
    )
    store["sp-b2"] = Row(
        doctype="Security Price",
        name="sp-b2",
        security="sec-bench-nifty",
        date=date(2026, 3, 1),
        close="23100",
        currency="INR",
        source="Stooq",
    )
    store["fx1"] = Row(
        doctype="FX Rate",
        name="fx1",
        from_currency="INR",
        to_currency="USD",
        date=date(2026, 1, 1),
        rate="0.012",
    )
    result = services_mod.benchmark_return("p1", "NIFTY50", as_of=date(2026, 3, 1))
    assert result["benchmark"] == "NIFTY50"
    assert result["benchmark_name"] == "Nifty 50 (India)"
    assert result["benchmark_currency"] == "INR"
    assert result["base_currency"] == "USD"
    assert result["note"] is None
    # 23100/22000 − 1 = 5%, unaffected by the (single) FX rate.
    assert result["benchmark_return_ytd"] == D("0.05")
    assert result["benchmark_start"] == "22000"
    assert result["benchmark_end"] == "23100"
    assert result["excess_return_ytd"] == D("-0.05")  # portfolio TWR 0 − benchmark 5%


def test_benchmark_return_reports_missing_prices_instead_of_fabricating(services):
    services_mod, fake, store = services
    result = services_mod.benchmark_return("p1", "SP500", as_of=date(2026, 3, 1))
    assert result["benchmark_return_ytd"] is None
    assert result["excess_return_ytd"] is None
    assert "no price" in result["note"]


def test_benchmark_return_fails_closed_without_fx_rate(services):
    services_mod, fake, store = services
    store["sp-b1"] = Row(
        doctype="Security Price",
        name="sp-b1",
        security="sec-bench-nifty",
        date=date(2026, 1, 2),
        close="22000",
        currency="INR",
        source="Stooq",
    )
    store["sp-b2"] = Row(
        doctype="Security Price",
        name="sp-b2",
        security="sec-bench-nifty",
        date=date(2026, 3, 1),
        close="23100",
        currency="INR",
        source="Stooq",
    )
    store["sec-bench-nifty"] = Row(
        doctype="Security",
        name="sec-bench-nifty",
        security_name="Nifty 50",
        ticker="BENCH:NIFTY50",
        asset_class="Benchmark",
        currency="INR",
        status="Active",
    )
    # No INR→USD rate in the book: the comparison is reported, not guessed.
    result = services_mod.benchmark_return("p1", "NIFTY50", as_of=date(2026, 3, 1))
    assert result["benchmark_return_ytd"] is None
    assert "No FX rate" in result["note"]


def test_benchmark_return_rejects_unknown_codes(services):
    services_mod, fake, store = services
    with pytest.raises(ValueError, match="Unknown benchmark"):
        services_mod.benchmark_return("p1", "MOON100", as_of=date(2026, 3, 1))


def test_benchmark_security_is_created_outside_license_class_count(services):
    services_mod, fake, store = services
    from frappe_investing.core import benchmarks

    name = services_mod._benchmark_security(benchmarks.get("SP500"))
    row = store[name]
    assert row.asset_class == "Benchmark"
    assert row.ticker == "BENCH:SP500"
    assert row.currency == "USD"
    # Idempotent: a second lookup returns the same row.
    assert services_mod._benchmark_security(benchmarks.get("SP500")) == name


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


def _cloud_mod(services, *, plan="", checked=None, secret="sk-test-secret"):
    """license_service with a Cloud subscription cache, and the real evaluate()."""
    import frappe_investing.license_service as license_mod

    _, fake, store = services
    if secret:
        fake.conf["sk_frappe_investing"] = secret
    store["Investment License"].update(cloud_plan=plan, cloud_checked_at=checked)
    return license_mod


def _press_returns(monkeypatch, **info):
    from frappe_investing import marketplace

    monkeypatch.setattr(marketplace, "fetch_subscription", lambda secret, **kw: info)


def test_a_cloud_plan_sets_the_tier_with_no_license_key(services):
    license_mod = _cloud_mod(services, plan="Pro", checked=NOW)
    state = license_mod.current_state()
    assert state.tier == "pro"
    assert state.source == "cloud"
    assert state.max_asset_classes is None
    assert services[2]["Investment License"].license_key == ""


def test_a_cloud_plan_survives_an_outage_inside_the_grace_window(services):
    license_mod = _cloud_mod(services, plan="Pro", checked=NOW - timedelta(days=6))
    assert license_mod.current_state().tier == "pro"


def test_a_cloud_plan_stops_counting_once_the_grace_window_lapses(services):
    license_mod = _cloud_mod(services, plan="Pro", checked=NOW - timedelta(days=8))
    state = license_mod.current_state()
    assert state.tier == "standard"
    assert state.max_asset_classes == 1


def test_resolving_a_tier_never_calls_frappe_cloud(services, monkeypatch):
    from frappe_investing import marketplace

    def explode(*args, **kw):
        raise AssertionError("enforcement must not block on the network")

    monkeypatch.setattr(marketplace, "fetch_subscription", explode)
    license_mod = _cloud_mod(services, plan="Pro", checked=NOW)
    license_mod.require_asset_class("Bond")  # no exception, no request


def test_refresh_stores_the_plan_press_reports(services, monkeypatch):
    license_mod = _cloud_mod(services)
    _press_returns(monkeypatch, plan="Pro", site="acme.frappe.cloud", enabled=True, document_name="x")
    public = license_mod.refresh_cloud_subscription()
    assert public["tier"] == "pro"
    assert public["source"] == "cloud"
    assert public["cloud_plan"] == "Pro"
    assert public["cloud_site"] == "acme.frappe.cloud"
    assert services[2]["Investment License"].cloud_checked_at == NOW


def test_refresh_clears_the_plan_when_the_subscription_is_disabled(services, monkeypatch):
    license_mod = _cloud_mod(services, plan="Pro", checked=NOW)
    _press_returns(monkeypatch, plan="Pro", site="acme.frappe.cloud", enabled=False)
    public = license_mod.refresh_cloud_subscription()
    assert public["cloud_plan"] == ""
    assert public["tier"] == "standard"
    assert "not active" in public["cloud_note"]


def test_a_renamed_paid_plan_still_grants_the_paid_tier(services, monkeypatch):
    # One paid plan on the listing: a name this build has not seen means the
    # listing was renamed, not that the customer stopped paying.
    license_mod = _cloud_mod(services)
    _press_returns(monkeypatch, plan="Investing $5", site="acme.frappe.cloud", enabled=True)
    public = license_mod.refresh_cloud_subscription()
    assert public["tier"] == "pro"
    assert public["source"] == "cloud"
    assert "Investing $5" in public["cloud_note"]


def test_a_free_plan_subscription_stays_on_the_free_limits(services, monkeypatch):
    license_mod = _cloud_mod(services)
    _press_returns(monkeypatch, plan="Free", site="acme.frappe.cloud", enabled=True)
    public = license_mod.refresh_cloud_subscription()
    assert public["tier"] == "standard"
    assert public["max_asset_classes"] == 1
    assert public["cloud_plan"] == "Free"


def test_a_failed_refresh_keeps_the_cached_plan_and_its_timestamp(services, monkeypatch):
    from frappe_investing import marketplace

    stale = NOW - timedelta(days=2)
    license_mod = _cloud_mod(services, plan="Pro", checked=stale)

    def unreachable(secret, **kw):
        raise marketplace.SubscriptionUnavailable("Could not reach Frappe Cloud: timed out")

    monkeypatch.setattr(marketplace, "fetch_subscription", unreachable)
    public = license_mod.refresh_cloud_subscription()
    assert public["tier"] == "pro"
    assert services[2]["Investment License"].cloud_checked_at == stale
    assert "Could not reach" in public["cloud_note"]


def test_refresh_on_a_self_hosted_site_says_it_is_not_on_frappe_cloud(services):
    license_mod = _cloud_mod(services, secret=None)
    public = license_mod.refresh_cloud_subscription()
    assert public["cloud_managed"] is False
    assert public["tier"] == "standard"
    assert "not a Frappe Cloud" in public["cloud_note"]


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
