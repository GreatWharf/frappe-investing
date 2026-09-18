"""Whitelisted UI methods. Every method enforces roles and company scoping server-side."""

import frappe

from . import importer, license_service, services, sync_service
from .core.money import dec


def _user():
    frappe.only_for(("Investment User", "Investment Manager", "System Manager"))
    return frappe.session.user


def _manager():
    frappe.only_for(("Investment Manager", "System Manager"))
    return frappe.session.user


def _check_portfolio(name, write=False):
    doc = frappe.get_doc("Portfolio", name)
    doc.check_permission("write" if write else "read")
    return doc


@frappe.whitelist()
def has_permission():
    return bool({"Investment User", "Investment Manager", "System Manager"}.intersection(frappe.get_roles()))


@frappe.whitelist()
def get_dashboard(portfolio=None, risk_free_rate=0):
    _user()
    settings = services.settings()
    # HTTP arguments arrive as strings; coerce with the money helper, not
    # flt(), whose float result the accounting guard rejects downstream.
    risk_free_rate = dec(risk_free_rate or 0)
    portfolios = frappe.get_all(
        "Portfolio", fields=["name", "portfolio_name", "company", "base_currency"], limit_page_length=200
    )
    if portfolio and portfolio not in {row.name for row in portfolios}:
        frappe.throw("Unknown portfolio.", frappe.PermissionError)
    portfolio = portfolio or (portfolios[0].name if portfolios else None)
    if not portfolio:
        return {
            "needs_setup": True,
            "license": license_service.public_state(),
            "usage": {"asset_classes_used": [], "value_check": None},
            "portfolios": portfolios,
            "settings": {
                "price_provider": settings.price_provider,
                "auto_accounting": settings.auto_accounting,
                "default_cost_method": settings.default_cost_method,
            },
        }
    _check_portfolio(portfolio)
    values = services.value_portfolio(portfolio)
    perf = services.performance_summary(portfolio, risk_free_rate=risk_free_rate)
    connections = frappe.get_all(
        "Broker Connection",
        filters={"company": frappe.db.get_value("Portfolio", portfolio, "company")},
        fields=["name", "connection_name", "broker", "status", "enabled", "last_sync", "last_error"],
    )
    pending = frappe.get_all(
        "Investment Event",
        filters={"accounting_status": ["in", ["Pending", "Failed"]], "docstatus": 1},
        fields=["name", "event_type", "posting_date", "security"],
        limit_page_length=50,
        order_by="posting_date desc",
    )
    recent = frappe.get_all(
        "Investment Event",
        filters={"docstatus": 1},
        fields=[
            "name",
            "event_type",
            "posting_date",
            "security",
            "qty",
            "price",
            "currency",
            "accounting_status",
        ],
        limit_page_length=15,
        order_by="posting_date desc",
    )
    accounts = frappe.get_all(
        "Investment Account",
        filters={"enabled": 1},
        fields=["name", "account_name", "portfolio", "currency"],
        limit_page_length=200,
        order_by="account_name asc",
    )
    return {
        "needs_setup": False,
        "portfolio": portfolio,
        "portfolios": portfolios,
        "values": values,
        "performance": perf,
        "connections": connections,
        "pending_accounting": pending,
        "recent_events": recent,
        "accounts": accounts,
        "license": license_service.public_state(),
        "usage": {
            "asset_classes_used": license_service.used_asset_classes(),
            "value_check": license_service.portfolio_value_check(values["total_value"], values["base"]),
        },
        "settings": {
            "price_provider": settings.price_provider,
            "auto_accounting": settings.auto_accounting,
            "default_cost_method": settings.default_cost_method,
        },
    }


@frappe.whitelist(methods=["POST"])
def create_portfolio(portfolio_name, company, base_currency=None):
    _manager()
    if not portfolio_name or len(portfolio_name) > 140:
        frappe.throw("Portfolio name must be 1–140 characters.")
    frappe.get_doc("Company", company).check_permission("read")
    doc = frappe.get_doc(
        {
            "doctype": "Portfolio",
            "portfolio_name": portfolio_name.strip(),
            "company": company,
            "base_currency": base_currency,
        }
    )
    doc.insert()
    return {"name": doc.name}


# Fields a Desk caller may set on a manual event. Everything else —
# journal_entry, accounting_status, connection, reversal_of, dedupe_key,
# meta_json — is engine-owned: accepting them from the client would let a
# caller forge accounting links, skip dedupe, or attach events to another
# source's sync connection.
_MANUAL_EVENT_FIELDS = frozenset(
    {
        "event_type",
        "posting_date",
        "account",
        "security",
        "qty",
        "price",
        "amount",
        "gross",
        "fees",
        "taxes",
        "accrued_interest",
        "currency",
        "split_ratio",
        "basis_allocation",
        "child_ratio",
        "child_security",
        "target_currency",
        "target_amount",
        "lot_ids",
        "source_ref",
        "target_account",
        "notes",
    }
)


@frappe.whitelist(methods=["POST"])
def record_manual_event(**data):
    _user()
    allowed = {key: data[key] for key in _MANUAL_EVENT_FIELDS if key in data}
    account = frappe.get_doc("Investment Account", allowed.get("account"))
    account.check_permission("write")
    # The account's company must match the caller's readable companies: a
    # write check on the account alone does not stop posting into another
    # company's portfolio through a shared account name.
    company = frappe.db.get_value("Portfolio", account.portfolio, "company")
    frappe.get_doc("Company", company).check_permission("read")
    name, created = services.record_event(dict(allowed, source="Manual"))
    return {"name": name, "created": created}


@frappe.whitelist(methods=["POST"])
def preview_event_accounting(**data):
    """Dry-run journal lines without persisting anything."""
    _manager()
    doc = frappe.get_doc({"doctype": "Investment Event", **data})
    doc._compute_dedupe()
    event = doc.to_core_event()
    event.validate()
    from .core import accounting
    from .core.fx import RateBook

    policy_name = frappe.db.get_value(
        "Investment Accounting Policy", {"company": services._company_of(doc)}, "name"
    )
    if not policy_name:
        return {"lines": [], "note": "No accounting policy configured for this company."}
    policy_doc = frappe.get_doc("Investment Accounting Policy", policy_name)
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
    book = RateBook()
    company_currency = frappe.get_cached_value("Company", services._company_of(doc), "default_currency")
    if event.currency != company_currency:
        rows = frappe.get_all(
            "FX Rate",
            filters={
                "from_currency": event.currency,
                "to_currency": company_currency,
                "date": ["<=", event.date],
            },
            fields=["rate"],
            order_by="date desc",
            limit_page_length=1,
        )
        if not rows:
            frappe.throw(f"No FX rate {event.currency}/{company_currency} on or before {event.date}.")
        book.set(event.currency, company_currency, event.date, rows[0].rate)
    try:
        lines = accounting.journal_lines(
            event,
            accounting.Policy(company_currency=company_currency, rules=rules),
            rates=book,
            on=event.date,
            allocations=[],
        )
    except accounting.UnmappedEvent as exc:
        return {"lines": [], "note": str(exc)}
    return {"lines": [line.__dict__ for line in lines]}


@frappe.whitelist(methods=["POST"])
def save_license(license_key):
    return license_service.save_license(license_key or "")


@frappe.whitelist(methods=["POST"])
def refresh_license():
    """Re-read the Frappe Cloud plan now, rather than waiting for the daily job."""
    _manager()
    return license_service.refresh_cloud_subscription()


@frappe.whitelist()
def license_status():
    _user()
    return license_service.public_state()


# ---------------------------------------------------------------- broker setup
@frappe.whitelist(methods=["POST"])
def zerodha_login_url(connection):
    _manager()
    doc = frappe.get_doc("Broker Connection", connection)
    doc.check_permission("write")
    if doc.broker != "Zerodha":
        frappe.throw("This connection is not a Zerodha connection.")
    return {"url": sync_service.connector_for(doc).login_url()}


@frappe.whitelist(methods=["POST"])
def zerodha_exchange_token(connection, request_token):
    _manager()
    doc = frappe.get_doc("Broker Connection", connection)
    doc.check_permission("write")
    connector = sync_service.connector_for(doc)
    token = connector.exchange_token(request_token)
    from frappe.utils.password import set_encrypted_password

    set_encrypted_password("Broker Connection", doc.name, token, "access_token")
    frappe.db.set_value(
        "Broker Connection", doc.name, {"access_token": "********", "status": "Connected", "last_error": ""}
    )
    return {"connected": True}


@frappe.whitelist(methods=["POST"])
def sync_now(connection):
    _manager()
    # frappe.enqueue returns None when deduplicate drops the job (already
    # queued/running) — report that honestly instead of a false queued: True.
    queued = frappe.enqueue(
        "frappe_investing.sync_service.sync_connection",
        name=connection,
        queue="long",
        job_id=f"investing-sync-{connection}",
        deduplicate=True,
    )
    if queued is None:
        return {"queued": False, "note": "A sync for this connection is already queued or running."}
    return {"queued": True}


@frappe.whitelist(methods=["POST"])
def refresh_prices(provider=None):
    _manager()
    return sync_service.refresh_prices(provider)


# ---------------------------------------------------------------- statement CSV import
@frappe.whitelist()
def import_template_url():
    """Download URL for the statement CSV template (headers + one example row)."""
    _user()
    return {"url": "/assets/frappe_investing/csv/statement_template.csv"}


@frappe.whitelist()
def import_reference():
    """Every supported event type with the columns each one requires.

    Powers the import dialog's reference tab; the required lists mirror
    core/events.py validate() so the docs and the validator cannot drift.
    """
    _user()
    return {"event_types": importer.OPERATIONS_REFERENCE}


@frappe.whitelist()
def import_cost_method(account):
    """Capital-gains cost-method backing for the import flow's selector."""
    _user()
    doc = frappe.get_doc("Investment Account", account)
    doc.check_permission("read")
    return importer.cost_method_info(account)


@frappe.whitelist(methods=["POST"])
def import_preview(account, csv_text, mapping=None):
    """Parse + validate only. Persists per-row errors as Import Error rows."""
    _user()
    account_doc = frappe.get_doc("Investment Account", account)
    account_doc.check_permission("write")
    import json

    mapping = json.loads(mapping) if isinstance(mapping, str) else (mapping or None)
    preview = importer.preview_import(csv_text, account=account, mapping=mapping)
    batch = _record_import_batch(
        account, preview["total_rows"], preview["valid_rows"], len(preview["errors"]),
        preview["errors"], status="Draft",
    )
    preview["batch"] = batch
    return preview


@frappe.whitelist(methods=["POST"])
def import_post(account, csv_text, mapping=None):
    """Post a validated CSV through services.record_event (dedupe applies).

    Refuses to post while any row has an error — never a partial commit.
    """
    _user()
    account_doc = frappe.get_doc("Investment Account", account)
    account_doc.check_permission("write")
    import json

    mapping = json.loads(mapping) if isinstance(mapping, str) else (mapping or None)
    try:
        result = importer.post_import(csv_text, account=account, mapping=mapping)
    except importer.ImportError as exc:
        preview = importer.preview_import(csv_text, account=account, mapping=mapping)
        _record_import_batch(
            account,
            preview["total_rows"],
            preview["valid_rows"],
            len(preview["errors"]) + len(exc.errors),
            [*preview["errors"], *exc.errors],
            status="Failed",
        )
        frappe.throw(str(exc))
    _record_import_batch(
        account, result["posted"], result["posted"],
        0, [], status="Imported",
    )
    return result


def _record_import_batch(account, total_rows, imported, failed, errors, status):
    """Persist one Import Batch with its per-row Import Error table."""
    frappe.flags.investing_internal = True
    try:
        batch = frappe.get_doc(
            {
                "doctype": "Import Batch",
                "source": "CSV Import",
                "account": account,
                "status": status,
                "total_rows": total_rows,
                "imported": imported,
                "failed": failed,
                "errors": [
                    {"row_no": error.get("row"), "message": error.get("message")} for error in errors
                ],
            }
        )
        batch.insert(ignore_permissions=True)
        return batch.name
    finally:
        frappe.flags.investing_internal = False


# ---------------------------------------------------------------- benchmarks

# ------------------------------------------------------- method cards
def _card_portfolio(*args, **kwargs):
    """Resolve which portfolio a Method-type Number Card is asking about.

    Desk invokes Method cards with varying conventions, so accept anything
    and look for a portfolio name; fall back to the first portfolio,
    mirroring allocation_chart.get_data.
    """
    seen = []

    def _collect(value):
        if isinstance(value, str):
            try:
                value = frappe.parse_json(value)
            except Exception:
                pass
        if isinstance(value, dict):
            seen.append(value.get("portfolio"))
            filters = value.get("filters")
            if isinstance(filters, dict):
                seen.append(filters.get("portfolio"))
            elif isinstance(filters, list):
                for item in filters:
                    if (
                        isinstance(item, (list, tuple))
                        and len(item) >= 4
                        and item[1] in ("portfolio", "name")
                    ):
                        seen.append(item[3])
        elif isinstance(value, str):
            seen.append(value)

    for arg in args:
        _collect(arg)
    _collect(kwargs)

    for name in seen:
        if isinstance(name, str) and frappe.db.exists("Portfolio", name):
            _check_portfolio(name)
            return name
    rows = frappe.get_all("Portfolio", pluck="name", limit_page_length=1)
    if rows:
        _check_portfolio(rows[0])
        return rows[0]
    return None


def _card_metric(key, *args, **kwargs):
    _user()
    name = _card_portfolio(*args, **kwargs)
    if not name:
        return 0
    value = services.performance_summary(name).get(key)
    return float(value) if value is not None else 0


@frappe.whitelist()
def portfolio_twr_ytd(*args, **kwargs):
    """YTD time-weighted return, backing the "TWR YTD" Number Card."""
    return _card_metric("twr_ytd", *args, **kwargs)


@frappe.whitelist()
def portfolio_xirr_ytd(*args, **kwargs):
    """YTD money-weighted (XIRR) return, backing the "XIRR YTD" Number Card."""
    return _card_metric("xirr_ytd", *args, **kwargs)


@frappe.whitelist()
def portfolio_sharpe_ytd(*args, **kwargs):
    """YTD Sharpe ratio, backing the "Sharpe YTD" Number Card."""
    return _card_metric("sharpe_ytd", *args, **kwargs)
@frappe.whitelist()
def benchmark_list():
    """The curated benchmark catalog, for the comparison picker."""
    _user()
    from .core import benchmarks

    return [
        {"code": b["code"], "name": b["name"], "currency": b["currency"]} for b in benchmarks.BENCHMARKS
    ]


@frappe.whitelist()
def compare_benchmark(portfolio, benchmark, risk_free_rate=0):
    """YTD portfolio performance against one benchmark index."""
    _user()
    _check_portfolio(portfolio)
    return services.benchmark_return(
        portfolio, benchmark, risk_free_rate=dec(risk_free_rate or 0)
    )


@frappe.whitelist(methods=["POST"])
def refresh_benchmark_prices(codes=None):
    _manager()
    import json

    code_list = json.loads(codes) if isinstance(codes, str) else codes
    return sync_service.refresh_benchmark_prices(code_list)
