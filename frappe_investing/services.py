"""Frappe boundary: events → lots → accounting → valuation → snapshots.

Positions are derived from submitted Investment Events. Corrections are guarded
cancellations, never silent edits. Every external write is idempotent by dedupe_key.
"""

from datetime import date

import frappe
from frappe.utils import today

from .core import accounting, performance, valuation
from .core.events import INCOME_EVENTS, LOT_EVENTS
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
            if existing.docstatus == 2:
                # Resync after cancel: the old row is dead — clear its key so
                # the re-posted event gets a fresh identity, then proceed to
                # insert below instead of returning the cancelled document.
                frappe.db.set_value("Investment Event", existing.name, "dedupe_key", None)
            else:
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
    """on_submit: move lots, record allocations, post accounting.

    Transfers move Tax Lot rows between accounts directly (see
    ``_persist_transfer_out``): the destination rows are written at submit
    time with status Open, so shares are never parked in the ephemeral
    engine buffer and no in-memory chaining is needed.
    """
    from .core.events import TRANSFER_OUT

    event = doc.to_core_event()
    result = None
    if event.type in LOT_EVENTS:
        engine = _engine_for(doc.account, doc.security)
        result = engine.apply(event)
        _persist_lots(doc, engine, result)
        if event.type == TRANSFER_OUT:
            _persist_transfer_out(doc, result.new_lots)
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


def _persist_transfer_out(doc, moved_lots):
    """Write the destination leg of a Transfer Out as real Tax Lot rows.

    The engine parks moved lots in its ephemeral ``transferred`` buffer, which
    dies with the per-event LotEngine — so without this, the source position
    drops and nothing is created anywhere. The destination is the event's
    ``target_account`` (required by _validate_transfer, same portfolio,
    enabled — re-checked here for engine-level callers). Rows carry the
    destination account, original cost/acquired dates, and
    ``source_event`` = the Transfer Out, so cost basis and history survive.
    A matching Transfer In event is not required and never was: posting one
    would double-count (its engine path would create the same rows again).
    """
    if not moved_lots:
        return
    dest = doc.get("target_account")
    if not dest:
        frappe.throw("Transfer Out requires a target account.")
    dest_portfolio = frappe.db.get_value("Investment Account", dest, "portfolio")
    if dest_portfolio != _portfolio_of(doc):
        frappe.throw("Transfer target must be in the same portfolio as the source account.")
    if frappe.db.get_value("Investment Account", dest, "enabled") != 1:
        frappe.throw("Transfer target account is not enabled.")
    frappe.flags.investing_internal = True
    try:
        for lot in moved_lots:
            if lot.qty <= 0:
                continue
            frappe.get_doc(
                {
                    "doctype": "Tax Lot",
                    "account": dest,
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


def _portfolio_of(doc):
    return frappe.db.get_value("Investment Account", doc.account, "portfolio")


def _company_of(doc):
    portfolio = _portfolio_of(doc)
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
        # An AVERAGE buy merges into a pre-existing lot in place (lots._buy):
        # no allocations and no new Tax Lot row, so un-merge it here. The merge
        # is linear (qty += buy_qty, unit_cost = total_cost / qty), so the
        # pre-image is the exact arithmetic inverse of the buy.
        if doc.event_type == "Buy" and cost_method_for(_portfolio_of(doc)) == "AVERAGE":
            _unmerge_average_buy(doc)
            return
        for lot in frappe.get_all("Tax Lot", filters={"source_event": doc.name}, pluck="name"):
            frappe.delete_doc("Tax Lot", lot, ignore_permissions=True)
    finally:
        frappe.flags.investing_internal = False


def _unmerge_average_buy(doc):
    """Restore the lot an AVERAGE buy merged into, then drop no rows.

    Merge math (lots._buy): lot.qty += buy_qty;
    lot.unit_cost = (old_qty * old_unit_cost + buy_qty * price + fees) / new_qty.
    Inverting: old_qty = new_qty - buy_qty;
    old_unit_cost = (new_qty * new_unit_cost - buy_qty * price - fees) / old_qty.
    """
    buy_qty = dec(doc.qty)
    buy_price, buy_fees = dec(doc.price), dec(doc.get("fees") or 0)
    merged = frappe.get_all(
        "Tax Lot",
        filters={"account": doc.account, "security": doc.security, "status": "Open"},
        fields=["name", "qty_open", "unit_cost"],
    )
    if len(merged) != 1:
        frappe.throw(
            f"Cannot cancel: expected one merged lot for {doc.security} in {doc.account}, "
            f"found {len(merged)}."
        )
    row = merged[0]
    old_qty = dec(row.qty_open) - buy_qty
    if old_qty <= 0:
        frappe.delete_doc("Tax Lot", row.name, ignore_permissions=True)
        return
    old_unit_cost = (dec(row.qty_open) * dec(row.unit_cost) - buy_qty * buy_price - buy_fees) / old_qty
    frappe.db.set_value(
        "Tax Lot",
        row.name,
        {"qty_open": str(old_qty), "unit_cost": str(old_unit_cost)},
    )


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
    prices, currencies, classes, labels = {}, {}, {}, {}
    for security in securities:
        info = frappe.db.get_value(
            "Security", security, ["currency", "asset_class", "security_name", "ticker"], as_dict=True
        )
        currencies[security] = info.currency
        classes[security] = info.asset_class
        labels[security] = (info.security_name or "", info.ticker or "")
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
    # Display labels ride along so the UI never has to show raw doc IDs.
    for security, bucket in result["by_security"].items():
        bucket["security_name"], bucket["ticker"] = labels.get(security, ("", ""))
    return result


def snapshot_portfolio(portfolio_name, day=None):
    """Persist a daily snapshot; idempotent per portfolio/day."""
    day = day or today()
    existing = frappe.db.get_value("Portfolio Snapshot", {"portfolio": portfolio_name, "date": day}, "name")
    portfolio_doc = frappe.get_doc("Portfolio", portfolio_name)
    base = portfolio_doc.base_currency or frappe.get_cached_value(
        "Company", portfolio_doc.company, "default_currency"
    )
    values = value_portfolio(portfolio_name, day)
    flows = _external_flows(
        portfolio_name, day, base_currency=base, rates=_rate_book(portfolio_doc.company)
    )
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


def _external_flows(portfolio_name, day, *, base_currency=None, rates=None):
    """Investor cash in/out for one day, in base currency.

    Only Deposit/Withdrawal move investor capital — Fee and FX Conversion
    are internal and never count as external flows.
    """
    accounts = frappe.get_all("Investment Account", filters={"portfolio": portfolio_name}, pluck="name")
    rows = frappe.get_all(
        "Investment Event",
        filters={
            "account": ["in", accounts or ["-"]],
            "posting_date": day,
            "docstatus": 1,
            "event_type": ["in", ["Deposit", "Withdrawal"]],
        },
        fields=["event_type", "amount", "currency"],
    )
    total = dec(0)
    for row in rows:
        sign = dec(1) if row.event_type == "Deposit" else dec(-1)
        amount = sign * dec(row.amount or 0)
        if base_currency and row.get("currency") and row.currency != base_currency:
            if rates is None:
                raise ValueError("An FX RateBook is required to convert external flows to base currency.")
            amount = q(rates.convert_on_or_before(amount, row.currency, base_currency, day), 2)
        total += amount
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


def performance_summary(portfolio_name, as_of=None, risk_free_rate=0):
    """YTD TWR + XIRR + Sharpe from snapshots and external flows."""
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
    base = _portfolio_base_currency(portfolio_name)
    book = _rate_book(_portfolio_company(portfolio_name))
    flows_raw = frappe.get_all(
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
        fields=["posting_date", "event_type", "amount", "currency"],
    )
    flows = []
    for row in flows_raw:
        amount = dec(row.amount or 0) if row.event_type == "Deposit" else -dec(row.amount or 0)
        if row.get("currency") and row.currency != base:
            amount = q(book.convert_on_or_before(amount, row.currency, base, row.posting_date), 2)
        flows.append((row.posting_date, -amount))
    terminal = snaps[-1].value if snaps else dec(0)
    try:
        xirr = performance.xirr(flows, terminal, date.fromisoformat(str(as_of))) if flows else None
    except ValueError:
        xirr = None
    realized, income_amt = _period_totals(portfolio_name, as_of)
    sharpe = performance.sharpe_ratio(snaps, risk_free_annual=risk_free_rate)
    return {
        "twr_ytd": twr,
        "xirr_ytd": xirr,
        "sharpe_ytd": sharpe,
        "risk_free_rate": dec(risk_free_rate),
        "realized_pnl_ytd": realized,
        "income_ytd": income_amt,
        "snapshots": len(snaps),
    }


def benchmark_return(portfolio_name, benchmark_code, as_of=None, risk_free_rate=0):
    """Portfolio YTD performance against a benchmark index over the same window.

    The benchmark's price return is expressed in the portfolio's base currency
    (FX-converted through the RateBook, like valuations). A benchmark with no
    usable price on the window's start or end is reported, not fabricated.
    """
    from .core import benchmarks

    as_of = as_of or today()
    bench = benchmarks.get(benchmark_code)
    if bench is None:
        raise InvestingError(f"Unknown benchmark: {benchmark_code}")
    summary = performance_summary(portfolio_name, as_of=as_of, risk_free_rate=risk_free_rate)
    base = _portfolio_base_currency(portfolio_name)
    start = date(date.fromisoformat(str(as_of)).year, 1, 1)
    security = _benchmark_security(bench)
    result = {
        "benchmark": bench["code"],
        "benchmark_name": bench["name"],
        "benchmark_currency": bench["currency"],
        "base_currency": base,
        "portfolio_twr_ytd": summary["twr_ytd"],
        "benchmark_return_ytd": None,
        "excess_return_ytd": None,
        "benchmark_start": None,
        "benchmark_end": None,
        "note": None,
    }
    start_price = _price_on(security, start, direction="gte")
    end_price = _price_on(security, as_of, direction="lte")
    if start_price is None or end_price is None:
        result["note"] = "Benchmark has no price on the start or end of this period yet."
        return result
    result["benchmark_start"] = str(start_price["close"])
    result["benchmark_end"] = str(end_price["close"])
    book = _rate_book(_portfolio_company(portfolio_name))
    try:
        start_base = book.convert_on_or_before(
            dec(start_price["close"]), bench["currency"], base, start_price["date"]
        )
        end_base = book.convert_on_or_before(
            dec(end_price["close"]), bench["currency"], base, end_price["date"]
        )
    except Exception:
        result["note"] = f"No FX rate to convert {bench['currency']} into {base}."
        return result
    bench_return = (end_base - start_base) / start_base
    result["benchmark_return_ytd"] = bench_return
    if summary["twr_ytd"] is not None:
        result["excess_return_ytd"] = dec(summary["twr_ytd"]) - bench_return
    return result


def _price_on(security, day, *, direction):
    """Nearest stored price on (or just inside) a date, never fabricated."""
    op = ">=" if direction == "gte" else "<="
    order = "date asc" if direction == "gte" else "date desc"
    rows = frappe.get_all(
        "Security Price",
        filters={"security": security, "date": [op, day]},
        fields=["close", "date"],
        order_by=order,
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _benchmark_security(bench):
    """Find or create the Security row a benchmark's prices attach to.

    Benchmarks use asset class "Benchmark", which is excluded from the
    license's class count and from portfolio holdings (they hold no lots).
    """
    existing = frappe.db.get_value("Security", {"ticker": f"BENCH:{bench['code']}"}, "name")
    if existing:
        return existing
    frappe.flags.investing_internal = True
    try:
        return (
            frappe.get_doc(
                {
                    "doctype": "Security",
                    "security_name": bench["name"],
                    "ticker": f"BENCH:{bench['code']}",
                    "asset_class": "Benchmark",
                    "currency": bench["currency"],
                    "status": "Active",
                }
            )
            .insert(ignore_permissions=True)
            .name
        )
    finally:
        frappe.flags.investing_internal = False


def _portfolio_base_currency(portfolio_name):
    portfolio = frappe.get_doc("Portfolio", portfolio_name)
    return portfolio.base_currency or frappe.get_cached_value(
        "Company", portfolio.company, "default_currency"
    )


def _portfolio_company(portfolio_name):
    return frappe.db.get_value("Portfolio", portfolio_name, "company")


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
            # Engine ids already written during THIS replay. Splits, stock
            # dividends, spin-offs and AVERAGE buys return the (mutated)
            # EXISTING lots as new_lots — persisting each sighting would
            # duplicate the position (Buy 100 + 2:1 split = rows of 100 AND
            # 200). One Open row per engine id; later sightings update it.
            replayed_ids = set()
            for row in rows:
                doc = frappe.get_doc("Investment Event", row.name)
                event = doc.to_core_event()
                if event.type not in LOT_EVENTS:
                    continue
                result = engine.apply(event)
                for lot in result.new_lots:
                    if lot.qty <= 0:
                        continue
                    if lot.id in replayed_ids:
                        row_name = frappe.db.get_value(
                            "Tax Lot", {"engine_id": lot.id, "status": "Open"}, "name"
                        )
                        if row_name:
                            frappe.db.set_value(
                                "Tax Lot",
                                row_name,
                                {"qty_open": str(lot.qty), "unit_cost": str(lot.unit_cost)},
                            )
                        continue
                    replayed_ids.add(lot.id)
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
