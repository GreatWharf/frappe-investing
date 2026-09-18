"""Offline reversal tests: cancel restores lots, allocations and journal entries.

Uses the fake-Frappe store pattern from tests/test_services.py. Fails before
the reverse_event coverage work; passes after.
"""

from datetime import date
from decimal import Decimal as D

import pytest

from tests.support import make_services_fixture  # noqa: E402

services = make_services_fixture(__name__)


def buy(services_mod, store, **kw):
    from tests.support import Row

    base = dict(
        doctype="Investment Event",
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
    base.update(kw)
    doc = Row(base)
    doc.store = store
    doc.insert()
    services_mod.apply_event(doc)
    return doc


def test_cancel_sell_restores_lot_qty_and_removes_allocations(services):
    from tests.support import event_doc

    services_mod, fake, store = services
    buy_doc = buy(services_mod, store)
    lots = [r for r in store.values() if r.get("doctype") == "Tax Lot"]
    assert len(lots) == 1 and D(lots[0].qty_open) == D(100)
    sell = event_doc(
        store,
        event_type="Sell",
        posting_date=date(2026, 2, 1),
        qty="40",
        price="15",
    )
    services_mod.apply_event(sell)
    assert D(lots[0].qty_open) == D(60)
    assert len([r for r in store.values() if r.get("doctype") == "Lot Allocation"]) == 1
    services_mod.reverse_event(sell)
    assert D(lots[0].qty_open) == D(100)
    assert lots[0].status == "Open"
    assert [r for r in store.values() if r.get("doctype") == "Lot Allocation"] == []
    assert buy_doc.name in store  # the buy itself is untouched


def test_cancel_buy_removes_lots_created_by_that_event(services):

    services_mod, fake, store = services
    doc = buy(services_mod, store)
    assert len([r for r in store.values() if r.get("doctype") == "Tax Lot"]) == 1
    services_mod.reverse_event(doc)
    assert [r for r in store.values() if r.get("doctype") == "Tax Lot"] == []


def test_cancel_submitted_journal_entry_cancels_it_first(services):
    from tests.support import Row

    services_mod, fake, store = services
    doc = buy(services_mod, store)
    # reverse_event resolves frappe at call time from sys.modules; wrapping
    # cancel on the stored row observes the JE cancellation directly.
    store["je-1"] = Row(doctype="Journal Entry", name="je-1", docstatus=1)
    cancelled = []
    je_row = store["je-1"]
    je_row.cancel = lambda: (cancelled.append("je-1"), je_row.update({"docstatus": 2}))
    doc.journal_entry = "je-1"
    services_mod.reverse_event(doc)
    assert cancelled == ["je-1"]
    assert [r for r in store.values() if r.get("doctype") == "Tax Lot"] == []


def test_cancel_refuses_when_a_later_event_exists(services):
    from tests.support import event_doc

    services_mod, fake, store = services
    first = buy(services_mod, store)
    # A later submitted event for the same account/security blocks the cancel.
    # (submit() runs apply_event, so insert the later event directly.)
    event_doc(
        store,
        event_type="Sell",
        posting_date=date(2026, 3, 1),
        qty="10",
        price="12",
        docstatus=1,
    )
    with pytest.raises(ValueError, match="later event"):
        services_mod.reverse_event(first)
