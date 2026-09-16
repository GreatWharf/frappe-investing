"""Whitelisted UI methods. Every method enforces roles and company scoping server-side."""

import frappe

from . import license_service, services, sync_service


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
    from frappe.utils import flt

    risk_free_rate = flt(risk_free_rate)
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
    return {
        "needs_setup": False,
        "portfolio": portfolio,
        "portfolios": portfolios,
        "values": values,
        "performance": perf,
        "connections": connections,
        "pending_accounting": pending,
        "recent_events": recent,
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


@frappe.whitelist(methods=["POST"])
def record_manual_event(**data):
    _user()
    account = frappe.get_doc("Investment Account", data.get("account"))
    account.check_permission("write")
    name, created = services.record_event(dict(data, source="Manual"))
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
    frappe.enqueue(
        "frappe_investing.sync_service.sync_connection",
        name=connection,
        queue="long",
        job_id=f"investing-sync-{connection}",
        deduplicate=True,
    )
    return {"queued": True}


@frappe.whitelist(methods=["POST"])
def refresh_prices(provider=None):
    _manager()
    return sync_service.refresh_prices(provider)


# ---------------------------------------------------------------- benchmarks
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
    from frappe.utils import flt

    return services.benchmark_return(
        portfolio, benchmark, risk_free_rate=flt(risk_free_rate)
    )


@frappe.whitelist(methods=["POST"])
def refresh_benchmark_prices(codes=None):
    _manager()
    import json

    code_list = json.loads(codes) if isinstance(codes, str) else codes
    return sync_service.refresh_benchmark_prices(code_list)
