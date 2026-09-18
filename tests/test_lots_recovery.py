"""Lots recovery: rebuild dedupe, SPECIFIC name translation, content dedupe.

Proves audit findings: rebuild_positions duplicating splits, SPECIFIC sells
matching hidden engine uuids, dedupe ignoring content + resync-after-cancel
returning the dead row. Transfer Out persistence is covered by
tests/test_reverse_event.py-style flows via services.apply_event below.
"""

from tests.support import Row, event_doc, make_services_fixture

services = make_services_fixture(__name__)


def _buy(services_mod, store, qty="100", price="10"):
    doc = event_doc(store, qty=qty, price=price)
    doc.submit()
    return doc


def test_rebuild_does_not_duplicate_split_lots(services):
    services_mod, fake, store = services
    _buy(services_mod, store)
    split = event_doc(store, event_type="Split", split_ratio="2", qty=None, price=None)
    split.submit()
    before = [r for r in store.values() if r.get("doctype") == "Tax Lot"]
    assert len(before) == 1 and before[0]["qty_open"] == "200"
    services_mod.rebuild_positions("a1", "sec-aapl")
    after = [
        r
        for r in store.values()
        if r.get("doctype") == "Tax Lot" and r.get("status") == "Open"
    ]
    assert len(after) == 1, f"split duplicated lots: {len(after)} open rows"
    assert after[0]["qty_open"] == "200"


def test_specific_sell_accepts_visible_tax_lot_names(services):
    services_mod, fake, store = services
    _buy(services_mod, store, qty="10", price="20")
    lot_name = next(r["name"] for r in store.values() if r.get("doctype") == "Tax Lot")
    assert not lot_name.startswith("lot-"), "test needs a real doc name, not an engine id"
    sell = event_doc(
        store, event_type="Sell", qty="5", price="25", lot_ids=lot_name
    )
    sell.submit()  # must not raise "requires valid open lots"
    remaining = [r for r in store.values() if r.get("doctype") == "Tax Lot"]
    assert sum(float(r["qty_open"]) for r in remaining) == 5.0


def test_broker_amendment_same_ref_new_content_reapplies(services):
    services_mod, fake, store = services
    first, created = services_mod.record_event(
        {
            "event_type": "Buy",
            "posting_date": "2026-01-05",
            "account": "a1",
            "security": "sec-aapl",
            "qty": "10",
            "price": "10",
            "currency": "USD",
            "source": "Zerodha",
            "source_ref": "trade-1",
        }
    )
    assert created
    # Broker corrects 10 -> 100 shares under the same ref: new content must
    # NOT dedupe against the original.
    second, created2 = services_mod.record_event(
        {
            "event_type": "Buy",
            "posting_date": "2026-01-05",
            "account": "a1",
            "security": "sec-aapl",
            "qty": "100",
            "price": "10",
            "currency": "USD",
            "source": "Zerodha",
            "source_ref": "trade-1",
        }
    )
    assert created2 and second != first


def test_resync_after_cancel_reapplies_instead_of_returning_dead_row(services):
    services_mod, fake, store = services
    name, created = services_mod.record_event(
        {
            "event_type": "Buy",
            "posting_date": "2026-01-05",
            "account": "a1",
            "security": "sec-aapl",
            "qty": "10",
            "price": "10",
            "currency": "USD",
            "source": "Zerodha",
            "source_ref": "trade-9",
        }
    )
    assert created
    store[name]["docstatus"] = 2  # cancelled
    again, created2 = services_mod.record_event(
        {
            "event_type": "Buy",
            "posting_date": "2026-01-05",
            "account": "a1",
            "security": "sec-aapl",
            "qty": "10",
            "price": "10",
            "currency": "USD",
            "source": "Zerodha",
            "source_ref": "trade-9",
        }
    )
    assert created2 and again != name


def test_transfer_out_moves_shares_to_target_account(services):
    services_mod, fake, store = services
    store["a2"] = Row(
        doctype="Investment Account", name="a2", account_name="Second",
        portfolio="p1", currency="USD", enabled=1,
    )
    _buy(services_mod, store, qty="50", price="10")
    out = event_doc(
        store, event_type="Transfer Out", qty="20", price=None,
        target_account="a2",
    )
    out.submit()
    src = sum(
        float(r["qty_open"]) for r in store.values()
        if r.get("doctype") == "Tax Lot" and r.get("account") == "a1"
    )
    dst = [
        r for r in store.values()
        if r.get("doctype") == "Tax Lot" and r.get("account") == "a2"
    ]
    assert src == 30.0
    assert len(dst) == 1 and float(dst[0]["qty_open"]) == 20.0
    assert float(dst[0]["unit_cost"]) == 10.0  # cost basis survives
