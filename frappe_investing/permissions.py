"""Company-scoped query restrictions: users see records for companies they can read."""

import frappe

COMPANY_SCOPED = {
    "Portfolio": "company",
    "Broker Connection": "company",
    "Investment Accounting Policy": "company",
    "Investment Account": ("portfolio", "Portfolio", "company"),
    "Portfolio Snapshot": ("portfolio", "Portfolio", "company"),
    "Import Batch": ("account", "Investment Account", "portfolio"),
    "Tax Lot": ("account", "Investment Account", "portfolio"),
    "Lot Allocation": ("event", "Investment Event", "account"),
    "Broker Sync Log": ("connection", "Broker Connection", "company"),
}

# Doctypes with no company link of their own. Security Price and FX Rate are
# global market data (no company field anywhere in the chain); the task's
# list names them via their parents (Tax Lot/Allocation/Snapshot/Sync
# Log/Import Batch carry the chain), so they stay unscoped here.
GLOBAL_MARKET_DATA = {"Security Price", "FX Rate"}


def _allowed_companies(user):
    rows = frappe.get_list("Company", pluck="name", limit_page_length=1000)  # permission-aware
    return rows or ["-"]


def scoped_query(user=None, doctype=None):
    field = COMPANY_SCOPED.get(doctype or "")
    if not field:
        return None
    user = user or frappe.session.user
    if user == "Administrator" or "System Manager" in frappe.get_roles(user):
        return None
    companies = _allowed_companies(user)
    quoted = ",".join(frappe.db.escape(c) for c in companies)
    return f"`tab{doctype}`.`{field}` IN ({quoted})"


def child_query(user=None, doctype=None):
    """Events/lots/snapshots scope through their account → portfolio → company chain."""
    if doctype == "Investment Event":
        companies = _allowed_companies(user or frappe.session.user)
        quoted = ",".join(frappe.db.escape(c) for c in companies)
        return (
            "`tabInvestment Event`.`account` IN (SELECT name FROM `tabInvestment Account` WHERE "
            "portfolio IN (SELECT name FROM `tabPortfolio` WHERE company IN (" + quoted + ")))"
        )
    spec = COMPANY_SCOPED.get(doctype or "")
    if isinstance(spec, tuple):
        link_field, link_doctype, hop = spec
        companies = _allowed_companies(user or frappe.session.user)
        quoted = ",".join(frappe.db.escape(c) for c in companies)
        return (
            f"`tab{doctype}`.`{link_field}` IN ("
            f"SELECT name FROM `tab{link_doctype}` WHERE "
            f"{_hop_condition(link_doctype, hop, quoted)})"
        )
    return None


def _hop_condition(link_doctype, hop, quoted):
    """SQL fragment restricting a linked parent row to the allowed companies."""
    if link_doctype in ("Portfolio", "Broker Connection"):
        return f"`tab{link_doctype}`.`{hop}` IN ({quoted})"
    if link_doctype == "Investment Account":
        return (
            "`tabInvestment Account`.`portfolio` IN "
            f"(SELECT name FROM `tabPortfolio` WHERE company IN ({quoted}))"
        )
    if link_doctype == "Investment Event":
        return (
            "`tabInvestment Event`.`account` IN "
            "(SELECT name FROM `tabInvestment Account` WHERE portfolio IN "
            f"(SELECT name FROM `tabPortfolio` WHERE company IN ({quoted})))"
        )
    return f"`tab{link_doctype}`.`name` IN ({quoted})"
