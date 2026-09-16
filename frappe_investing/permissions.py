"""Company-scoped query restrictions: users see portfolios for companies they can read."""

import frappe

COMPANY_SCOPED = {
    "Portfolio": "company",
    "Broker Connection": "company",
    "Investment Accounting Policy": "company",
}


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
    return None
