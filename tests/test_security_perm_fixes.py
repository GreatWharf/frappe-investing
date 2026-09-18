"""Security/permission hardening: tier re-gating, Benchmark enforcement, allowlists.

Covers the CONFIRMED fixes that run through ManagedDocument validation,
record_manual_event allowlisting, list scoping, and the CoinGecko gate:

1.  tier gate re-checks on asset_class change and Delisted -> Active
    reactivation (documents._retracks_class / _validate_security).
2.  Buy/event posting against Benchmark-class securities is blocked
    (benchmarks hold no lots — services.py _benchmark_security comment).
4.  INTERNAL_SAFE guard is insert-only no longer: direct Desk updates to
    engine fields (qty_open/unit_cost/...) need the investing_internal flag.
5.  record_manual_event allowlists event fields (strips journal_entry,
    accounting_status, connection, reversal_of, dedupe_key, meta_json) and
    checks the account's company.
9.  COMPANY_SCOPED covers Investment Account / Tax Lot / Lot Allocation /
    Portfolio Snapshot / Broker Sync Log / Import Batch; child_query
    resolves each through its link chain.
10. CoinGecko uses a real gate (sync_service.crypto_gate) and only requires
    the Crypto class when crypto prices are actually requested.
3/6/7/8 are doctype-JSON permission changes, asserted in
test_doctype_perm_tables.py against the JSON on disk.
"""

import pytest

from tests.support import Row, evict_app_modules, fake_frappe, seed, store  # noqa: E402


@pytest.fixture
def docs(monkeypatch):
    seed(store)
    fake = fake_frappe(monkeypatch)
    import frappe_investing.documents as documents

    yield documents, fake, store
    evict_app_modules()


def free_tier(monkeypatch):
    """Pin the license path to the free tier (1 asset class)."""
    import frappe_investing.license_service as license_mod
    from frappe_investing import licensing

    real = licensing.evaluate
    monkeypatch.setattr(licensing, "evaluate", lambda key="", **kw: real("", **kw))
    monkeypatch.setattr(license_mod, "evaluate", lambda key="", **kw: licensing.evaluate("", **kw))


def security_doc(**kw):
    base = dict(
        doctype="Security",
        security_name=kw.get("security_name", "X"),
        asset_class=kw.get("asset_class", "Stock"),
        currency="USD",
        status="Active",
    )
    base.update(kw)
    doc = Row(base)
    doc.is_new = lambda: False
    doc.get_doc_before_save = lambda: None
    doc.has_value_changed = lambda field: False
    return doc


def managed(documents, doc):
    """Rewrap a harness Row as the real ManagedDocument, keeping test stubs."""
    real = documents.ManagedDocument(dict(doc))
    for key, value in doc.items():
        if key not in real or callable(value):
            real[key] = value
    real.is_new = doc.is_new
    real.has_value_changed = doc.has_value_changed
    real.get_doc_before_save = doc.get_doc_before_save
    return real


def managed_event(documents, doc):
    """Rewrap an event Row; validation only needs is_new on top of Row data."""
    real = documents.ManagedDocument(dict(doc))
    real.is_new = doc.is_new
    return real


# ---------------------------------------------------------------- (1) re-gating
def test_asset_class_change_rechecks_the_tier(docs, monkeypatch):
    documents, fake, store = docs
    free_tier(monkeypatch)  # Stock already tracked by sec-aapl
    doc = security_doc(name="sec-aapl", asset_class="Bond")
    doc.has_value_changed = lambda field: field == "asset_class"
    with pytest.raises(PermissionError):
        documents.ManagedDocument._validate_security(managed(documents, doc))


def test_same_class_save_passes_without_recheck(docs, monkeypatch):
    documents, fake, store = docs
    free_tier(monkeypatch)
    doc = security_doc(name="sec-aapl", asset_class="Stock")
    documents.ManagedDocument._validate_security(managed(documents, doc))  # no exception


def test_delisted_to_active_reactivation_rechecks_the_tier(docs, monkeypatch):
    documents, fake, store = docs
    free_tier(monkeypatch)
    doc = security_doc(name="sec-bond", asset_class="Bond", status="Active")
    before = Row(dict(doctype="Security", asset_class="Bond", status="Delisted"))
    doc.get_doc_before_save = lambda: before
    with pytest.raises(PermissionError):
        documents.ManagedDocument._validate_security(managed(documents, doc))


def test_delisted_row_staying_delisted_does_not_recheck(docs, monkeypatch):
    documents, fake, store = docs
    free_tier(monkeypatch)
    doc = security_doc(name="sec-fund", asset_class="Fund", status="Delisted")
    before = Row(dict(doctype="Security", asset_class="Fund", status="Delisted"))
    doc.get_doc_before_save = lambda: before
    documents.ManagedDocument._validate_security(managed(documents, doc))  # no exception


def test_benchmark_never_rechecks(docs, monkeypatch):
    documents, fake, store = docs
    free_tier(monkeypatch)
    doc = security_doc(name="sec-b", asset_class="Benchmark")
    doc.has_value_changed = lambda field: True
    documents.ManagedDocument._validate_security(managed(documents, doc))  # no exception


# ------------------------------------------------------- (2) Benchmark events
def event_doc(**kw):
    base = dict(
        doctype="Investment Event",
        event_type="Buy",
        posting_date="2026-01-05",
        account="a1",
        security="sec-bench",
        qty="10",
        price="100",
        currency="USD",
        source="Manual",
    )
    base.update(kw)
    doc = Row(base)
    doc.is_new = lambda: True
    return doc


def test_buy_against_benchmark_security_is_blocked(docs):
    documents, fake, store = docs
    store["sec-bench"] = Row(
        doctype="Security", name="sec-bench", asset_class="Benchmark", status="Active"
    )
    doc = event_doc()
    with pytest.raises(PermissionError, match="Benchmark"):
        documents.ManagedDocument._validate_event(managed_event(documents, doc))


def test_buy_against_stock_security_passes(docs):
    documents, fake, store = docs
    doc = event_doc(security="sec-aapl")
    documents.ManagedDocument._validate_event(managed_event(documents, doc))  # no exception


# ------------------------------------------------- (4) engine-field Desk guard
def engine_doc(doctype, **kw):
    doc = Row(dict(doctype=doctype, **kw))
    doc.is_new = lambda: False
    return doc


def test_direct_desk_update_to_engine_field_is_blocked(docs):
    documents, fake, store = docs
    doc = engine_doc("Tax Lot", name="lot-1", qty_open="100", unit_cost="10")
    doc.has_value_changed = lambda field: field == "qty_open"
    with pytest.raises(PermissionError, match="qty_open"):
        documents.ManagedDocument._guard(managed(documents, doc))


def test_engine_update_with_internal_flag_passes(docs):
    documents, fake, store = docs
    fake.flags.investing_internal = True
    try:
        doc = engine_doc("Tax Lot", name="lot-1", qty_open="100")
        doc.has_value_changed = lambda field: True
        documents.ManagedDocument._guard(managed(documents, doc))  # no exception
    finally:
        fake.flags.investing_internal = False


def test_non_engine_field_update_passes(docs):
    documents, fake, store = docs
    doc = engine_doc("Tax Lot", name="lot-1", qty_open="100")
    doc.has_value_changed = lambda field: False
    documents.ManagedDocument._guard(managed(documents, doc))  # no exception


def test_engine_insert_still_blocked(docs):
    documents, fake, store = docs
    doc = engine_doc("Tax Lot", qty_open="100")
    doc.is_new = lambda: True
    with pytest.raises(PermissionError):
        documents.ManagedDocument._guard(managed(documents, doc))


# ------------------------------------------------- (5) manual-event allowlist
@pytest.fixture
def api(monkeypatch):
    seed(store)
    fake = fake_frappe(monkeypatch)
    import frappe_investing.api as api_mod

    yield api_mod, fake, store
    evict_app_modules()


def test_record_manual_event_strips_engine_fields(api, monkeypatch):
    api_mod, fake, store = api
    seen = []
    monkeypatch.setattr(api_mod.services, "record_event", lambda data: (seen.append(data) or ("EV-1", True)))
    api_mod.record_manual_event(
        account="a1",
        event_type="Buy",
        security="sec-aapl",
        qty="10",
        price="10",
        currency="USD",
        journal_entry="JE-1",
        accounting_status="Posted",
        connection="conn-1",
        reversal_of="EV-0",
        dedupe_key="forged",
        meta_json='{"forged": true}',
    )
    payload = seen[0]
    for forged in (
        "journal_entry",
        "accounting_status",
        "connection",
        "reversal_of",
        "dedupe_key",
        "meta_json",
    ):
        assert forged not in payload, forged
    assert payload["source"] == "Manual"
    assert payload["qty"] == "10"


def test_record_manual_event_checks_account_company(api, monkeypatch):
    api_mod, fake, store = api
    monkeypatch.setattr(api_mod.services, "record_event", lambda data: ("EV-1", True))
    store["evil-co"] = Row(doctype="Company", name="evil-co", default_currency="USD")

    class NoRead(Row):
        def check_permission(self, perm):
            if perm == "read":
                raise PermissionError("no read")

    store["Evil Co"] = NoRead(doctype="Company", name="Evil Co")
    store["pevil"] = Row(doctype="Portfolio", name="pevil", company="Evil Co", base_currency="USD")
    store["a-evil"] = Row(doctype="Investment Account", name="a-evil", portfolio="pevil")
    real_get_doc = fake.get_doc

    def get_doc(kind, name=None, **kw):
        if isinstance(kind, dict):
            doc = Row(kind)
            doc.store = store
            return doc
        if kind == "Company" and name == "Evil Co":
            return store["Evil Co"]
        return real_get_doc(kind, name, **kw)

    fake.get_doc = get_doc
    with pytest.raises(PermissionError):
        api_mod.record_manual_event(account="a-evil", event_type="Buy")


# ----------------------------------------------------------------- (9) scoping
def test_company_scoped_covers_the_listed_doctypes(monkeypatch):
    seed(store)
    fake = fake_frappe(monkeypatch)
    fake.get_list = lambda dt, **kw: ["Acme"]
    try:
        from frappe_investing import permissions

        for doctype in (
            "Investment Account",
            "Tax Lot",
            "Lot Allocation",
            "Portfolio Snapshot",
            "Broker Sync Log",
            "Import Batch",
        ):
            assert doctype in permissions.COMPANY_SCOPED, doctype
            assert permissions.child_query(doctype=doctype) is not None, doctype
    finally:
        evict_app_modules()


def test_child_query_chains_through_links(monkeypatch):
    seed(store)
    fake = fake_frappe(monkeypatch)
    fake.get_list = lambda dt, **kw: ["Acme"]
    try:
        from frappe_investing import permissions

        lot = permissions.child_query(doctype="Tax Lot")
        assert "tabInvestment Account" in lot and "tabPortfolio" in lot
        alloc = permissions.child_query(doctype="Lot Allocation")
        assert "tabInvestment Event" in alloc
        snap = permissions.child_query(doctype="Portfolio Snapshot")
        assert "tabPortfolio" in snap
    finally:
        evict_app_modules()


# ------------------------------------------------------------ (10) crypto gate
def test_crypto_gate_is_a_real_function(monkeypatch):
    seed(store)
    fake_frappe(monkeypatch)
    try:
        import frappe_investing.sync_service as sync_mod

        assert sync_mod.crypto_gate("crypto") in (True, False)
        assert sync_mod.crypto_gate("stocks") is False
        assert sync_mod.crypto_gate("anything-else") is False
    finally:
        evict_app_modules()


def test_free_tier_without_crypto_holdings_does_not_require_crypto(monkeypatch):
    seed(store)
    fake_frappe(monkeypatch)
    try:
        import frappe_investing.sync_service as sync_mod

        free_tier(monkeypatch)
        assert sync_mod.crypto_holdings_requested() is False
    finally:
        evict_app_modules()


def test_crypto_holdings_requested_when_active_crypto_mapped(monkeypatch):
    seed(store)
    fake_frappe(monkeypatch)
    try:
        import frappe_investing.sync_service as sync_mod

        store["sec-btc"] = Row(
            doctype="Security",
            name="sec-btc",
            asset_class="Crypto",
            status="Active",
            coingecko_id="bitcoin",
        )
        assert sync_mod.crypto_holdings_requested() is True
    finally:
        evict_app_modules()
