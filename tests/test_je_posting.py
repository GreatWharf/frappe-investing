"""Offline JE-posting tests: every _apply_accounting branch with a stubbed policy/JE layer.

Covers Off/Skipped, missing policy, UnmappedEvent failure, Not Applicable for
unmapped types, and the Draft-vs-Submit posting paths. Fails before the JE
wiring coverage; passes after.
"""

from datetime import date
from decimal import Decimal as D

from tests.support import make_services_fixture  # noqa: E402

services = make_services_fixture(__name__)


def policy(store, mode="Submit"):
    from tests.support import Row

    store["Acme-Cash"] = Row(doctype="Account", name="Acme-Cash")
    store["Acme-Equity"] = Row(doctype="Account", name="Acme-Equity")
    store["Acme-Gains"] = Row(doctype="Account", name="Acme-Gains")
    store["pol-1"] = Row(
        doctype="Investment Accounting Policy",
        name="pol-1",
        company="Acme",
        mode=mode,
        mappings=[
            Row(event_type="Buy", debit_account="Acme-Equity", credit_account="Acme-Cash"),
            Row(
                event_type="Sell",
                debit_account="Acme-Cash",
                credit_account="Acme-Equity",
                pnl_account="Acme-Gains",
            ),
        ],
    )


def buy_doc(store, **kw):
    from tests.support import event_doc

    base = dict(
        event_type="Buy",
        posting_date=date(2026, 1, 5),
        qty="10",
        price="10",
        currency="USD",
    )
    base.update(kw)
    return event_doc(store, **base)


def test_no_policy_marks_event_skipped(services):
    services_mod, fake, store = services
    doc = buy_doc(store)
    services_mod.apply_event(doc)
    assert store[doc.name].get("accounting_status") == "Skipped"
    assert [r for r in store.values() if r.get("doctype") == "Journal Entry"] == []


def test_policy_off_marks_event_skipped(services):
    services_mod, fake, store = services
    policy(store, mode="Off")
    doc = buy_doc(store)
    services_mod.apply_event(doc)
    assert store[doc.name].get("accounting_status") == "Skipped"
    assert [r for r in store.values() if r.get("doctype") == "Journal Entry"] == []


def test_buy_posts_and_links_journal_entry(services):
    services_mod, fake, store = services
    policy(store)
    doc = buy_doc(store)
    services_mod.apply_event(doc)
    jes = [r for r in store.values() if r.get("doctype") == "Journal Entry"]
    assert len(jes) == 1
    assert store[doc.name].get("accounting_status") == "Posted"
    assert store[doc.name].get("journal_entry") == jes[0].name
    assert jes[0].docstatus == 1  # Submit mode submits the JE
    assert jes[0].company == "Acme"
    assert len(jes[0].accounts) == 2


def test_draft_mode_leaves_journal_entry_unsubmitted(services):
    services_mod, fake, store = services
    policy(store, mode="Draft")
    doc = buy_doc(store)
    services_mod.apply_event(doc)
    jes = [r for r in store.values() if r.get("doctype") == "Journal Entry"]
    assert len(jes) == 1
    assert store[doc.name].get("accounting_status") == "Posted"
    assert jes[0].get("docstatus", 0) != 1


def test_unmapped_event_type_marks_failed_with_note(services):
    services_mod, fake, store = services
    policy(store)
    first = buy_doc(store)
    services_mod.apply_event(first)
    sell = buy_doc(store, event_type="Sell", posting_date=date(2026, 2, 1), qty="10", price="15")
    # Sell with no Sell rule in the policy: Failed with a note naming the event.
    store["pol-1"].mappings = [m for m in store["pol-1"].mappings if m.event_type == "Buy"]
    services_mod.apply_event(sell)
    assert store[sell.name].get("accounting_status") == "Failed"
    assert "Sell" in (store[sell.name].get("notes") or "")


def test_non_accounting_event_marks_not_applicable(services):
    services_mod, fake, store = services
    policy(store)
    doc = buy_doc(store, event_type="Deposit", amount="100")
    doc.qty = None
    doc.price = None
    services_mod.apply_event(doc)
    assert store[doc.name].get("accounting_status") == "Not Applicable"


def test_sell_posts_gain_through_pnl_account(services):
    services_mod, fake, store = services
    policy(store)
    first = buy_doc(store)
    services_mod.apply_event(first)
    sell = buy_doc(store, event_type="Sell", posting_date=date(2026, 2, 1), qty="10", price="15")
    services_mod.apply_event(sell)
    assert store[sell.name].get("accounting_status") == "Posted"
    jes = [r for r in store.values() if r.get("doctype") == "Journal Entry"]
    assert len(jes) == 2  # one per event
    accounts = {line["account"] for line in jes[-1].accounts}
    assert "Acme-Gains" in accounts
    assert D(store[sell.name].get("journal_entry") and "1") == D(1)  # link set
