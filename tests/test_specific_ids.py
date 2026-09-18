"""SPECIFIC-ID sells accept the Tax Lot names users actually see.

Regression test: the core engine matches lot_ids against Lot.id (the hidden
engine_id), but Desk shows only Tax Lot document names. Entering the visible
name used to fail with "requires valid open lots". documents._resolve_lot_ids
translates names -> engine ids at the boundary; engine ids still pass through.
"""

from datetime import date
from decimal import Decimal as D

import pytest

from tests.support import Row, evict_app_modules, fake_frappe, seed, store  # noqa: E402


@pytest.fixture
def docs(monkeypatch):
    seed(store)
    fake = fake_frappe(monkeypatch)
    import frappe_investing.documents as documents

    yield documents, fake, store
    evict_app_modules()


def _buy(services_mod, store, **kw):
    base = dict(
        doctype="Investment Event",
        event_type="Buy",
        posting_date=date(2026, 1, 5),
        account="a1",
        security="sec-aapl",
        qty="10",
        price="10",
        currency="USD",
        source="Manual",
        docstatus=1,
    )
    base.update(kw)
    doc = Row(base)
    doc.store = store
    doc.insert()
    services_mod.apply_event(doc)
    return doc


def _doc_names(services_mod, store, cost):
    store["p1"].update({"cost_method": "SPECIFIC"})
    _buy(services_mod, store, posting_date=date(2026, 1, 5), price="10")
    _buy(services_mod, store, posting_date=date(2026, 1, 6), price=cost, source_ref="b2")
    lots = sorted(
        [r for r in store.values() if r.get("doctype") == "Tax Lot"],
        key=lambda r: D(r.unit_cost),
    )
    assert [str(r.unit_cost) for r in lots] == ["10", cost]
    return lots


def test_specific_sell_accepts_visible_tax_lot_names(docs, monkeypatch):
    documents, fake, store = docs
    import frappe_investing.services as services_mod

    lots = _doc_names(services_mod, store, "20")
    expensive = next(r for r in lots if str(r.unit_cost) == "20")

    sell = Row(
        dict(
            doctype="Investment Event",
            event_type="Sell",
            posting_date=date(2026, 2, 1),
            account="a1",
            security="sec-aapl",
            qty="5",
            price="25",
            currency="USD",
            source="Manual",
            lot_ids=expensive.name,  # the visible hash name, not the engine id
            docstatus=0,
        )
    )
    resolved = documents.ManagedDocument._resolve_lot_ids(sell)
    assert resolved == (expensive.engine_id,)
    sell["lot_ids"] = ",".join(resolved)
    sell.store = store
    sell.insert()
    services_mod.apply_event(sell)
    allocs = [r for r in store.values() if r.get("doctype") == "Lot Allocation"]
    assert len(allocs) == 1
    assert D(allocs[0].realized_pnl) == D(25)  # 5 x (25 - 20): the named lot


def test_specific_sell_still_accepts_engine_ids(docs):
    documents, fake, store = docs
    import frappe_investing.services as services_mod

    lots = _doc_names(services_mod, store, "20")
    expensive = next(r for r in lots if str(r.unit_cost) == "20")

    sell = Row(
        dict(
            doctype="Investment Event",
            event_type="Sell",
            posting_date=date(2026, 2, 1),
            account="a1",
            security="sec-aapl",
            qty="5",
            price="25",
            currency="USD",
            source="Manual",
            lot_ids=expensive.engine_id,
            docstatus=0,
        )
    )
    assert documents.ManagedDocument._resolve_lot_ids(sell) == (expensive.engine_id,)


def test_specific_sell_unknown_name_passes_through_to_engine_error(docs, monkeypatch):
    documents, fake, store = docs
    import frappe_investing.services as services_mod

    _doc_names(services_mod, store, "20")
    sell = Row(
        dict(
            doctype="Investment Event",
            event_type="Sell",
            posting_date=date(2026, 2, 1),
            account="a1",
            security="sec-aapl",
            qty="5",
            price="25",
            currency="USD",
            source="Manual",
            lot_ids="TAXLOT-NOPE",
            docstatus=0,
        )
    )
    assert documents.ManagedDocument._resolve_lot_ids(sell) == ("TAXLOT-NOPE",)
    sell.store = store
    sell.insert()
    with pytest.raises(ValueError, match="valid open lots"):
        services_mod.apply_event(sell)
