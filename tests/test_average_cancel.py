"""Cancelling an AVERAGE-method buy restores the pre-merge lot.

Regression test: reverse_event used to be a silent no-op for AVERAGE buys
because the buy merges into the existing lot in place (no allocations, no new
Tax Lot row), so there was nothing to restore or delete. Fails on the old
code (position stays 20 @105); passes with the _unmerge_average_buy fix.
"""

from datetime import date
from decimal import Decimal as D

from tests.support import Row, make_services_fixture  # noqa: E402

services = make_services_fixture(__name__)


def average_buy(services_mod, store, **kw):
    base = dict(
        doctype="Investment Event",
        event_type="Buy",
        posting_date=date(2026, 1, 5),
        account="a1",
        security="sec-aapl",
        qty="10",
        price="100",
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


def test_cancel_average_buy_restores_pre_merge_lot(services):
    services_mod, fake, store = services
    store["p1"].update({"cost_method": "AVERAGE"})
    first = average_buy(services_mod, store)
    lots = [r for r in store.values() if r.get("doctype") == "Tax Lot"]
    assert len(lots) == 1 and D(lots[0].qty_open) == D(10)

    second = average_buy(
        services_mod, store, posting_date=date(2026, 2, 1), price="110", source_ref="second"
    )
    assert D(lots[0].qty_open) == D(20)
    assert D(lots[0].unit_cost) == D(105)

    services_mod.reverse_event(second)
    assert D(lots[0].qty_open) == D(10)
    assert D(lots[0].unit_cost) == D(100)
    assert lots[0].status == "Open"
    assert first.name in store  # the original buy is untouched
    assert [r for r in store.values() if r.get("doctype") == "Lot Allocation"] == []


def test_cancel_first_average_buy_removes_the_lot(services):
    services_mod, fake, store = services
    store["p1"].update({"cost_method": "AVERAGE"})
    first = average_buy(services_mod, store)
    assert len([r for r in store.values() if r.get("doctype") == "Tax Lot"]) == 1
    services_mod.reverse_event(first)
    assert [r for r in store.values() if r.get("doctype") == "Tax Lot"] == []
