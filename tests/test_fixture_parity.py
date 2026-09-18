"""Fixture drift test: the offline Row.to_core_event must match production.

tests/support.Row.to_core_event is a copy of
frappe_investing/documents.py ManagedDocument.to_core_event. If production
grows a field (lot_ids, target_*, meta, notes) and the fixture does not
follow, sells silently pin FIFO and FX legs vanish. This test fails when
they drift. Includes a SPECIFIC cost-method override test proving lot_ids
survive the fixture.
"""

from datetime import date
from decimal import Decimal as D

from tests.support import make_services_fixture  # noqa: E402

services = make_services_fixture(__name__)


def production_event(**overrides):
    """Build the Event exactly the way documents.ManagedDocument.to_core_event does."""
    import json

    from frappe_investing.core.events import Event
    from frappe_investing.core.money import dec

    values = dict(
        event_type="Sell",
        posting_date=date(2026, 2, 1),
        account="a1",
        currency="USD",
        security="sec-aapl",
        qty="10",
        price="15",
        amount=None,
        gross=None,
        fees="1",
        taxes="0",
        accrued_interest="0",
        split_ratio=None,
        basis_allocation=None,
        child_ratio=None,
        child_security=None,
        target_currency="EUR",
        target_amount="9",
        lot_ids="LOT-1, LOT-2",
        source="Manual",
        source_ref="",
        notes="desk note",
        meta_json='{"desk": true}',
    )
    values.update(overrides)

    def d(field):
        value = values.get(field)
        return None if value in (None, "") else dec(str(value))

    return Event(
        type=values["event_type"],
        date=values["posting_date"],
        account=values["account"],
        currency=values["currency"],
        security=values["security"],
        qty=d("qty"),
        price=d("price"),
        amount=d("amount"),
        gross=d("gross"),
        fees=d("fees") or dec(0),
        taxes=d("taxes") or dec(0),
        accrued_interest=d("accrued_interest") or dec(0),
        split_ratio=d("split_ratio"),
        basis_allocation=d("basis_allocation"),
        child_ratio=d("child_ratio"),
        child_security=values["child_security"],
        target_currency=values["target_currency"],
        target_amount=d("target_amount"),
        lot_ids=tuple(x.strip() for x in (values["lot_ids"] or "").split(",") if x.strip()),
        source=values["source"],
        source_ref=values["source_ref"] or "",
        notes=values["notes"] or "",
        meta=json.loads(values["meta_json"] or "{}"),
    )


def test_offline_fixture_matches_production_to_core_event(services):
    from tests.support import Row

    row = Row(
        doctype="Investment Event",
        name="ev-1",
        event_type="Sell",
        posting_date=date(2026, 2, 1),
        account="a1",
        currency="USD",
        security="sec-aapl",
        qty="10",
        price="15",
        fees="1",
        taxes="0",
        accrued_interest="0",
        target_currency="EUR",
        target_amount="9",
        lot_ids="LOT-1, LOT-2",
        source="Manual",
        notes="desk note",
        meta_json='{"desk": true}',
    )
    assert row.to_core_event() == production_event()


def test_specific_cost_method_override_uses_lot_ids(services):
    """A portfolio cost_method=SPECIFIC must honor lot_ids end to end."""
    services_mod, fake, store = services
    from tests.support import event_doc

    store["p1"].cost_method = "SPECIFIC"
    for i, cost in enumerate(("10", "20")):
        doc = event_doc(
            store,
            event_type="Buy",
            posting_date=date(2026, 1, 5 + i),
            qty="10",
            price=cost,
        )
        services_mod.apply_event(doc)
    lots = sorted(
        [r for r in store.values() if r.get("doctype") == "Tax Lot"],
        key=lambda r: D(r.unit_cost),
    )
    assert [str(r.unit_cost) for r in lots] == ["10", "20"]
    expensive = next(r for r in lots if str(r.unit_cost) == "20")
    sell = event_doc(
        store,
        event_type="Sell",
        posting_date=date(2026, 2, 1),
        qty="5",
        price="25",
        lot_ids=expensive.engine_id,
    )
    services_mod.apply_event(sell)
    allocs = [r for r in store.values() if r.get("doctype") == "Lot Allocation"]
    assert len(allocs) == 1
    assert D(allocs[0].realized_pnl) == D(25)  # 5 x (25 - 20): the named lot, not FIFO
