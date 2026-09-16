"""Frappe boundary: events → lots → accounting → valuation → snapshots.

Positions are derived from submitted Investment Events. Corrections are guarded
cancellations, never silent edits. Every external write is idempotent by dedupe_key.
"""

from datetime import date

import frappe
from frappe.utils import today

from .core import accounting, performance, valuation
from .core.events import CASH_EVENTS, INCOME_EVENTS, LOT_EVENTS
from .core.fx import RateBook
from .core.lots import LotEngine
from .core.money import dec, q


class InvestingError(ValueError):
    pass


def settings():
    return frappe.get_single("Investment Settings")


def cost_method_for(portfolio_name):
    override = frappe.db.get_value("Portfolio", portfolio_name, "cost_method")
    return override or settings().default_cost_method or "FIFO"


# ---------------------------------------------------------------- events
def record_event(data, *, submit=True):
    """Create (idempotently) and submit an Investment Event from a normalized dict."""
    doc = frappe.get_doc({"doctype": "Investment Event", **data})
    doc._compute_dedupe()
    if doc.dedupe_key:
        existing = frappe.db.get_value(
            "Investment Event", {"dedupe_key": doc.dedupe_key}, ["name", "docstatus"], as_dict=True
        )
        if existing:
            return existing.name, False
    doc.insert()
    if submit:
        doc.submit()
    return doc.name, True


def _engine_for(account_name, security_name):
    """Rebuild an engine from persisted open lots for one account/security."""
    account = frappe.get_doc("Investment Account", account_name)
    engine = LotEngine(method=cost_method_for(account.portfolio))
    for row in frappe.get_all(
        "Tax Lot",
        filters={"account": account_name, "security": security_name, "status": "Open"},
        fields=["name", "qty_open", "unit_cost", "currency", "acquired", "source_event", "engine_id"],
    ):
        from .core.lots import Lot

        lot = Lot(
            security=security_name,
            account=account_name,
            qty=dec(row.qty_open),
            unit_cost=dec(row.unit_cost),
            currency=row.currency,
            acquired=row.acquired,
            source_ref=row.source_event or "",
            id=row.engine_id or row.name,
        )
        engine._lots.setdefault((account_name, security_name), []).append(lot)
    return engine


def apply_event(doc):
    """on_submit: move lots, record allocations, post accounting."""
    event = doc.to_core_event()
    result = None
    if event.type in LOT_EVENTS:
        engine = _engine_for(doc.account, doc.security)
        result = engine.apply(event)
        _persist_lots(doc, engine, result)
    if event.type in INCOME_EVENTS or event.type in {"Buy", "Sell", "Cash-in-lieu", "Redemption", "Fee"}:
        _apply_accounting(doc, event, result.allocations if result else None)
    else:
        frappe.db.set_value(doc.doctype, doc.name, "accounting_status", "Not Applicable")


def _persist_lots(doc, engine, result):
    frappe.flags.investing_internal = True
    try:
        current = {lot.id: lot for lot in engine._lots.get((doc.account, doc.security), [])}
        existing = {
            row.name: row
            for row in frappe.get_all(
                "Tax Lot",
                filters={"account": doc.account, "security": doc.security},
                fields=["name", "engine_id", "qty_open", "status"],
            )
        }
        for name, row in existing.items():
            lot = current.get(row.engine_id or name)
            if lot is None or lot.qty <= 0:
                frappe.db.set_value("Tax Lot", name, {"status": "Closed", "qty_open": "0"})
            elif dec(str(lot.qty)) != dec(row.qty_open):
                frappe.db.set_value(
                    "Tax Lot", name, {"qty_open": str(lot.qty), "unit_cost": str(lot.unit_cost)}
                )
        known_engine_ids = {row.engine_id for row in existing.values() if row.engine_id}
        for lot in current.values():
            if lot.id not in known_engine_ids and lot.id not in existing:
                frappe.get_doc(
                    {
                        "doctype": "Tax Lot",
                        "account": doc.account,
                        "security": doc.security,
                        "qty_open": str(lot.qty),
                        "qty_original": str(lot.qty),
                        "unit_cost": str(lot.unit_cost),
                        "currency": lot.currency,
                        "acquired": lot.acquired,
                        "source_event": doc.name,
                        "status": "Open",
                        "engine_id": lot.id,
                    }
                ).insert(ignore_permissions=True)
        for allocation in result.allocations:
            lot_name = frappe.db.get_value("Tax Lot", {"engine_id": allocation.lot_id}, "name")
            frappe.get_doc(
                {
                    "doctype": "Lot Allocation",
                    "event": doc.name,
                    "lot": lot_name,
                    "qty": str(allocation.qty),
                    "cost": str(q(allocation.cost, 4)),
                    "proceeds": str(q(allocation.proceeds, 4)),
                    "realized_pnl": str(q(allocation.realized_pnl, 4)),
                    "acquired": allocation.acquired,
                }
            ).insert(ignore_permissions=True)
    finally:
        frappe.flags.investing_internal = False


def _apply_accounting(doc, event, allocations):
    policy = frappe.db.get_value(
        "Investment Accounting Policy", {"company": _company_of(doc)}, ["name", "mode"], as_dict=True
    )
    if not policy or policy.mode == "Off":
        frappe.db.set_value(doc.doctype, doc.name, "accounting_status", "Skipped")
        return
    policy_doc = frappe.get_doc("Investment Accounting Policy", policy.name)
    rules = {
        row.event_type: accounting.Rule(
            debit=row.debit_account,
            credit=row.credit_account,
            tax_debit=row.tax_debit_account,
            fee_debit=row.fee_debit_account,
            pnl_account=row.pnl_account,
        )
        for row in policy_doc.mappings
    }
    company_currency = frappe.get_cached_value("Company", _company_of(doc), "default_currency")
    rates = _rate_book(_company_of(doc))
    try:
        lines = accounting.journal_lines(
            event,
            accounting.Policy(company_currency=company_currency, rules=rules),
            rates=rates,
            on=event.date,
            allocations=allocations,
        )
    except accounting.UnmappedEvent as exc:
        frappe.db.set_value(
            doc.doctype, doc.name, {"accounting_status": "Failed", "notes": (doc.notes or "") + f" {exc}"}
        )
        frappe.log_error(title="Investment accounting mapping failed", message=str(exc))
        return
    if not lines:
        frappe.db.set_value(doc.doctype, doc.name, "accounting_status", "Not Applicable")
        return
    je = frappe.get_doc(
        {
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": _company_of(doc),
            "posting_date": event.date,
            "user_remark": f"Investment {event.type}: {doc.name}",
            "accounts": [
                {
                    "account": line.account,
                    "debit_in_account_currency": line.debit,
                    "credit_in_account_currency": line.credit,
                }
                for line in lines
            ],
        }
    )
    je.insert(ignore_permissions=False)
    if policy_doc.mode == "Submit":
        je.submit()
    frappe.db.set_value(doc.doctype, doc.name, {"journal_entry": je.name, "accounting_status": "Posted"})


def _company_of(doc):
    portfolio = frappe.db.get_value("Investment Account", doc.account, "portfolio")
    return frappe.db.get_value("Portfolio", portfolio, "company") if portfolio else None


def _rate_book(company):
    book = RateBook()
    for row in frappe.get_all(
        "FX Rate",
        fields=["from_currency", "to_currency", "date", "rate"],
        order_by="date desc",
        limit_page_length=2000,
    ):
        book.set(row.from_currency, row.to_currency, row.date, dec(row.rate))
    return book


def reverse_event(doc):
    """Cancel an event: only the latest event for its account/security can be cancelled."""
    later = frappe.db.get_value(
        "Investment Event",
        {
            "account": doc.account,
            "security": doc.security,
            "docstatus": 1,
            "posting_date": [">", doc.posting_date],
        },
        "name",
    )
    if later:
        frappe.throw(f"Cannot cancel: later event {later} exists. Cancel it first.")
    if doc.event_type not in {
        "Buy",
        "Sell",
        "Cash-in-lieu",
        "Redemption",
        "Dividend",
        "Coupon",
        "Interest",
        "Fee",
        "Deposit",
        "Withdrawal",
    }:
        frappe.throw(
            "Corporate actions and transfers cannot be cancelled piecemeal. "
            "Cancel later events and ask a manager to rebuild positions for this security."
        )
    if doc.journal_entry:
        je = frappe.get_doc("Journal Entry", doc.journal_entry)
        if je.docstatus == 1:
            je.cancel()
    frappe.flags.investing_internal = True
    try:
        for allocation in frappe.get_all(
            "Lot Allocation", filters={"event": doc.name}, fields=["name", "lot", "qty"]
        ):
            lot = frappe.get_doc("Tax Lot", allocation.lot, for_update=True)
            lot.qty_open = str(dec(lot.qty_open) + dec(allocation.qty))
            lot.status = "Open"
            lot.save(ignore_permissions=True)
            frappe.delete_doc("Lot Allocation", allocation.name, ignore_permissions=True)
        for lot in frappe.get_all("Tax Lot", filters={"source_event": doc.name}, pluck="name"):
            frappe.delete_doc("Tax Lot", lot, ignore_permissions=True)
    finally:
        frappe.flags.investing_internal = False


# ---------------------------------------------------------------- valuation
def value_portfolio(portfolio_name, day=None):
    day = day or today()
    portfolio = frappe.get_doc("Portfolio", portfolio_name)
    accounts = frappe.get_all(
        "Investment Account", filters={"portfolio": portfolio_name, "enabled": 1}, pluck="name"
    )
    base = portfolio.base_currency or frappe.get_cached_value(
        "Company", portfolio.company, "default_currency"
    )
    engine = LotEngine(method=cost_method_for(portfolio_name))
    for row in frappe.get_all(
        "Tax Lot",
        filters={"account": ["in", accounts] or ["-"], "status": "Open"},
        fields=["account", "security", "qty_open", "unit_cost", "currency", "acquired", "engine_id"],
    ):
        from .core.lots import Lot

        engine._lots.setdefault((row.account, row.security), []).append(
            Lot(
                security=row.security,
                account=row.account,
                qty=dec(row.qty_open),
                unit_cost=dec(row.unit_cost),
                currency=row.currency,
                acquired=row.acquired,
                id=row.engine_id or row.name,
            )
        )
    securities = [s for (_a, s) in engine._lots]
    prices, currencies, classes = {}, {}, {}
    for security in securities:
        info = frappe.db.get_value("Security", security, ["currency", "asset_class"], as_dict=True)
        currencies[security] = info.currency
        classes[security] = info.asset_class
        rows = frappe.get_all(
            "Security Price",
            filters={"security": security, "date": ["<=", day]},
            fields=["close"],
            order_by="date desc",
            limit_page_length=1,
        )
        if rows:
            prices[(security, day)] = dec(rows[0].close)
    result = valuation.positions_value(
        engine,
        prices,
        day,
        base=base,
        fx=_rate_book(portfolio.company),
        asset_classes=classes,
        currencies=currencies,
    )
    result["allocation_by_class"] = valuation.allocation(result["by_security"], "asset_class")
    return result


def snapshot_portfolio(portfolio_name, day=None):
    """Persist a daily snapshot; idempotent per portfolio/day."""
    day = day or today()
    existing = frappe.db.get_value("Portfolio Snapshot", {"portfolio": portfolio_name, "date": day}, "name")
    values = value_portfolio(portfolio_name, day)
    flows = _external_flows(portfolio_name, day)
    realized, income_amt = _period_totals(portfolio_name, day)
    data = {
        "portfolio": portfolio_name,
        "date": day,
        "base_currency": frappe.db.get_value("Portfolio", portfolio_name, "base_currency"),
        "total_value": str(values["total_value"]),
        "total_cost": str(values["total_cost"]),
        "unrealized_pnl": str(values["unrealized_pnl"]),
        "realized_pnl_period": str(realized),
        "income_period": str(income_amt),
        "external_flow": str(flows),
        "holdings": [
            {
                "security": sec,
                "qty": str(bucket["qty"]),
                "cost": str(bucket["cost"]),
                "value": None if bucket["market_value"] is None else str(bucket["market_value"]),
                "unrealized_pnl": str(bucket["unrealized_pnl"]),
                "currency": bucket.get("currency"),
            }
            for sec, bucket in values["by_security"].items()
        ],
    }
    frappe.flags.investing_internal = True
    try:
        if existing:
            doc = frappe.get_doc("Portfolio Snapshot", existing)
            doc.update({key: value for key, value in data.items() if key != "holdings"})
            doc.set("holdings", data["holdings"])
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.get_doc({"doctype": "Portfolio Snapshot", **data})
            doc.insert(ignore_permissions=True)
    finally:
        frappe.flags.investing_internal = False
    return doc.name


def _external_flows(portfolio_name, day):
    accounts = frappe.get_all("Investment Account", filters={"portfolio": portfolio_name}, pluck="name")
    rows = frappe.get_all(
        "Investment Event",
        filters={
            "account": ["in", accounts or ["-"]],
            "posting_date": day,
            "docstatus": 1,
            "event_type": ["in", list(CASH_EVENTS)],
        },
        fields=["event_type", "amount"],
    )
    total = dec(0)
    for row in rows:
        sign = dec(1) if row.event_type in {"Deposit"} else dec(-1)
        total += sign * dec(row.amount or 0)
    return total


def _period_totals(portfolio_name, day):
    start = date(date.fromisoformat(str(day)).year, 1, 1)
    accounts = frappe.get_all("Investment Account", filters={"portfolio": portfolio_name}, pluck="name")
    realized = dec(0)
    for row in frappe.get_all(
        "Lot Allocation",
        filters={
            "event": [
                "in",
                frappe.get_all(
                    "Investment Event",
                    filters={
                        "account": ["in", accounts or ["-"]],
                        "docstatus": 1,
                        "posting_date": ["between", [start, day]],
                    },
                    pluck="name",
                )
                or ["-"],
            ]
        },
        pluck="realized_pnl",
    ):
        realized += dec(row)
    income_amt = dec(0)
    for row in frappe.get_all(
        "Investment Event",
        filters={
            "account": ["in", accounts or ["-"]],
            "docstatus": 1,
            "event_type": ["in", list(INCOME_EVENTS)],
            "posting_date": ["between", [start, day]],
        },
        fields=["gross", "taxes", "fees"],
    ):
        income_amt += dec(row.gross or 0) - dec(row.taxes or 0) - dec(row.fees or 0)
    return realized, income_amt


def performance_summary(portfolio_name, as_of=None):
    """YTD TWR + XIRR from snapshots and external flows."""
    as_of = as_of or today()
    start = date(date.fromisoformat(str(as_of)).year, 1, 1)
    snaps = [
        performance.Snapshot(
            day=row.date, value=dec(row.total_value), external_flow=dec(row.external_flow or 0)
        )
        for row in frappe.get_all(
            "Portfolio Snapshot",
            filters={"portfolio": portfolio_name, "date": ["between", [start, as_of]]},
            fields=["date", "total_value", "external_flow"],
            order_by="date asc",
        )
    ]
    twr = performance.twr(snaps)
    flows = [
        (row.posting_date, -_flow_sign(row))
        for row in frappe.get_all(
            "Investment Event",
            filters={
                "account": [
                    "in",
                    frappe.get_all("Investment Account", filters={"portfolio": portfolio_name}, pluck="name")
                    or ["-"],
                ],
                "event_type": ["in", ["Deposit", "Withdrawal"]],
                "docstatus": 1,
                "posting_date": ["between", [start, as_of]],
            },
            fields=["posting_date", "event_type", "amount"],
        )
    ]
    terminal = snaps[-1].value if snaps else dec(0)
    try:
        xirr = performance.xirr(flows, terminal, date.fromisoformat(str(as_of))) if flows else None
    except ValueError:
        xirr = None
    return {"twr_ytd": twr, "xirr_ytd": xirr, "snapshots": len(snaps)}


def _flow_sign(row):
    return dec(row.amount) if row.event_type == "Deposit" else -dec(row.amount)


def rebuild_positions(account_name, security_name):
    """Manager escape hatch: replay all submitted events for one account/security.

    Non-destructive: existing open lots are Closed (quantities preserved for audit)
    and the replayed state is written as fresh Open lots. Historical Lot Allocations
    keep pointing at the original lots, so realized-P&L history is never rewritten.
    Journal Entry links on events are untouched — accounting never replays.
    """
    frappe.only_for(("Investment Manager", "System Manager"))
    lock = frappe.cache.lock(
        f"investing-rebuild:{frappe.local.site}:{account_name}:{security_name}",
        timeout=300,
        blocking_timeout=0,
    )
    if not lock.acquire(blocking=False):
        raise InvestingError("A rebuild is already running for this account/security.")
    try:
        rows = frappe.get_all(
            "Investment Event",
            filters={"account": account_name, "security": security_name, "docstatus": 1},
            fields=["name"],
            order_by="posting_date asc, creation asc",
        )
        engine = LotEngine(
            method=cost_method_for(frappe.db.get_value("Investment Account", account_name, "portfolio"))
        )
        frappe.flags.investing_internal = True
        frappe.flags.investing_rebuild = True
        try:
            for lot_name in frappe.get_all(
                "Tax Lot",
                filters={"account": account_name, "security": security_name, "status": "Open"},
                pluck="name",
            ):
                frappe.db.set_value("Tax Lot", lot_name, "status", "Closed")
            for row in rows:
                doc = frappe.get_doc("Investment Event", row.name)
                event = doc.to_core_event()
                if event.type not in LOT_EVENTS:
                    continue
                result = engine.apply(event)
                for lot in result.new_lots:
                    if lot.qty <= 0:
                        continue
                    frappe.get_doc(
                        {
                            "doctype": "Tax Lot",
                            "account": account_name,
                            "security": security_name,
                            "qty_open": str(lot.qty),
                            "qty_original": str(lot.qty),
                            "unit_cost": str(lot.unit_cost),
                            "currency": lot.currency,
                            "acquired": lot.acquired,
                            "source_event": doc.name,
                            "status": "Open",
                            "engine_id": lot.id,
                        }
                    ).insert(ignore_permissions=True)
        finally:
            frappe.flags.investing_rebuild = False
            frappe.flags.investing_internal = False
        return {"replayed": len(rows)}
    finally:
        try:
            lock.release()
        except Exception:
            pass


def daily_snapshots():
    for name in frappe.get_all("Portfolio", pluck="name"):
        try:
            snapshot_portfolio(name)
        except Exception as exc:
            frappe.log_error(title=f"Snapshot failed for {name}", message=str(exc))
