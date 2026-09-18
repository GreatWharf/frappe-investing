"""Behavioral tier-gate tests: insert Securities through real validation.

Instead of asserting on an error-message substring, these tests drive
ManagedDocument._validate_security with the offline harness stubbing only
frappe itself. A second asset class on the free tier raises PermissionError
through validation; same-class and Benchmark inserts pass.
"""

import pytest

from tests.support import evict_app_modules, fake_frappe, seed, store  # noqa: E402


@pytest.fixture
def tier(monkeypatch):
    seed(store)
    fake = fake_frappe(monkeypatch)
    import frappe_investing.documents as documents
    import frappe_investing.license_service as license_mod

    yield documents, license_mod, fake, store
    evict_app_modules()

def security_row(store, **kw):
    from tests.support import Row

    base = dict(
        doctype="Security",
        name=kw.get("name", "sec-x"),
        security_name=kw.get("security_name", "X"),
        asset_class=kw.get("asset_class", "Stock"),
        currency="USD",
        status="Active",
    )
    base.update(kw)
    doc = Row(base)
    doc.is_new = lambda: True
    return doc


def free_state(monkeypatch, license_mod):
    from frappe_investing import licensing

    real = licensing.evaluate
    monkeypatch.setattr(licensing, "evaluate", lambda key="", **kw: real("", **kw))
    monkeypatch.setattr(
        license_mod, "evaluate", lambda key="", **kw: licensing.evaluate("", **kw)
    )


def test_second_asset_class_blocked_through_validation(tier, monkeypatch):
    documents, license_mod, fake, store = tier
    free_state(monkeypatch, license_mod)
    doc = security_row(store, name="sec-bond", security_name="Bond Co", asset_class="Bond")
    with pytest.raises(PermissionError):
        documents.ManagedDocument._validate_security(doc)


def test_same_class_passes_through_validation(tier, monkeypatch):
    documents, license_mod, fake, store = tier
    free_state(monkeypatch, license_mod)
    doc = security_row(store, name="sec-more", security_name="More Stock", asset_class="Stock")
    documents.ManagedDocument._validate_security(doc)  # no exception


def test_benchmark_class_never_counts_against_the_tier(tier, monkeypatch):
    documents, license_mod, fake, store = tier
    free_state(monkeypatch, license_mod)
    doc = security_row(store, name="sec-bench", security_name="Nifty", asset_class="Benchmark")
    documents.ManagedDocument._validate_security(doc)  # no exception
