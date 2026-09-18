"""Offline transfer tests: Transfer Out moves Tax Lot rows between accounts.

Before the fix, apply_event persisted only the source account's engine lots,
so a Transfer Out dropped the source position and created nothing anywhere
(the moved lots died in the ephemeral engine.transferred buffer).

Uses the shared fake-Frappe harness (tests/support.py). Fails before the
_persist_transfer_out work; passes after.
"""

from datetime import date
from decimal import Decimal as D

from tests.support import Row, make_services_fixture  # noqa: E402

services = make_services_fixture(__name__)


def _second_account(store):
    store["a2"] = Row(
        doctype="Investment Account",
        name="a2",
        account_name="IB Second",
        portfolio="p1",
        currency="USD",
        enabled=1,
    )


def buy(services_mod, store):
    doc = Row(
        dict(
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
    )
    doc.store = store
    doc.insert()
    services_mod.apply_event(doc)
    return doc


def transfer_out(services_mod, store, **kw):
    base = dict(
        doctype="Investment Event",
        event_type="Transfer Out",
        posting_date=date(2026, 2, 1),
        account="a1",
        security="sec-aapl",
        qty="50",
        currency="USD",
        source="Manual",
        target_account="a2",
        docstatus=1,
    )
    base.update(kw)
    doc = Row(base)
    doc.store = store
    doc.insert()
    services_mod.apply_event(doc)
    return doc


def open_lots(store, account):
    return [
        r
        for r in store.values()
        if r.get("doctype") == "Tax Lot" and r.account == account and r.status == "Open"
    ]


def test_transfer_out_moves_half_the_position(services):
    services_mod, fake, store = services
    _second_account(store)
    buy(services_mod, store)
    transfer_out(services_mod, store)

    src = open_lots(store, "a1")
    dest = open_lots(store, "a2")
    assert sum(D(r.qty_open) for r in src) == D(50)
    assert sum(D(r.qty_open) for r in dest) == D(50)
    # Cost basis and acquisition date travel with the shares.
    assert dest[0].unit_cost == src[0].unit_cost == "10"
    assert dest[0].engine_id == src[0].engine_id


def test_transfer_out_needs_a_target_account(services):
    import pytest

    services_mod, fake, store = services
    _second_account(store)
    buy(services_mod, store)
    with pytest.raises(ValueError, match="target account"):
        transfer_out(services_mod, store, target_account=None)


def test_transfer_out_to_unknown_account_fails(services):
    import pytest

    services_mod, fake, store = services
    _second_account(store)
    buy(services_mod, store)
    with pytest.raises(ValueError, match="Transfer target"):
        transfer_out(services_mod, store, target_account="nope")


def test_transfer_out_more_than_held_fails(services):
    import pytest

    services_mod, fake, store = services
    _second_account(store)
    buy(services_mod, store)
    with pytest.raises(ValueError, match="Insufficient quantity"):
        transfer_out(services_mod, store, qty="500")


def test_rebuild_after_transfer_keeps_both_legs(services):
    services_mod, fake, store = services
    _second_account(store)
    buy(services_mod, store)
    transfer_out(services_mod, store)
    # A rebuild replays only this account/security's events, so the source
    # leg replays to 50 and the destination rows (written by the Out) stay.
    assert sum(D(r.qty_open) for r in open_lots(store, "a1")) == D(50)
    assert sum(D(r.qty_open) for r in open_lots(store, "a2")) == D(50)
