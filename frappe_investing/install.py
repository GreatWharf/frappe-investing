"""Install lifecycle: version gates, roles, workspace, indexes, settings defaults."""

import frappe

USER_ROLES = ("Investment User", "Investment Manager")


def check_versions():
    import erpnext

    if frappe.__version__.split(".")[0] not in {"15", "16"} or erpnext.__version__.split(".")[0] not in {
        "15",
        "16",
    }:
        frappe.throw("Investing targets matching Frappe/ERPNext v15 or v16.")
    if frappe.db.db_type != "mariadb":
        frappe.throw("This release targets MariaDB-backed sites.")


def _roles():
    for name in USER_ROLES:
        if not frappe.db.exists("Role", name):
            frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert(
                ignore_permissions=True
            )


def _workspace():
    if frappe.db.exists("Workspace", "Investing"):
        return
    frappe.get_doc(
        {
            "doctype": "Workspace",
            "name": "Investing",
            "label": "Investing",
            "title": "Investing",
            "module": "Investing",
            "public": 1,
            "is_hidden": 0,
            "icon": "chart",
            "roles": [{"role": role} for role in (*USER_ROLES, "System Manager")],
            "shortcuts": [
                {"label": "Dashboard", "type": "Page", "link_to": "investing-dashboard"},
                {"label": "Broker Setup", "type": "Page", "link_to": "broker-setup"},
                {"label": "Portfolios", "type": "DocType", "link_to": "Portfolio", "doc_view": "List"},
                {"label": "Securities", "type": "DocType", "link_to": "Security", "doc_view": "List"},
                {"label": "Events", "type": "DocType", "link_to": "Investment Event", "doc_view": "List"},
                {
                    "label": "Connections",
                    "type": "DocType",
                    "link_to": "Broker Connection",
                    "doc_view": "List",
                },
                {
                    "label": "Accounting Policy",
                    "type": "DocType",
                    "link_to": "Investment Accounting Policy",
                    "doc_view": "List",
                },
                {"label": "License", "type": "DocType", "link_to": "Investment License", "doc_view": "Form"},
            ],
            "content": "[]",
        }
    ).insert(ignore_permissions=True)


def after_install():
    check_versions()
    _roles()
    settings = frappe.get_single("Investment Settings")
    if not settings.company:
        default_company = frappe.db.get_single_value("Global Defaults", "default_company")
        if default_company:
            settings.company = default_company
            settings.save(ignore_permissions=True)
    license_doc = frappe.get_single("Investment License")
    frappe.flags.investing_internal = True
    license_doc.save(ignore_permissions=True)
    frappe.flags.investing_internal = False
    after_migrate()


def after_migrate():
    check_versions()
    _roles()
    for doctype, fields, name in (
        ("Investment Event", ["account", "security", "posting_date"], "investing_event_account_security"),
        ("Investment Event", ["dedupe_key"], "investing_event_dedupe"),
        ("Tax Lot", ["account", "security", "status"], "investing_lot_open"),
        ("Security Price", ["security", "date"], "investing_price_date"),
        ("Portfolio Snapshot", ["portfolio", "date"], "investing_snapshot_day"),
    ):
        frappe.db.add_index(doctype, fields, name)
    _workspace()


def before_uninstall():
    if frappe.db.exists("Investment Event", {"docstatus": 1}):
        frappe.throw(
            "Cancel submitted events (and their Journal Entries) before uninstalling, "
            "or archive the site instead. Investment history is accounting data."
        )
