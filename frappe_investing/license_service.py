"""License storage and feature gating at the Frappe boundary."""

import frappe
from frappe.utils import now_datetime

from .licensing import evaluate


def current_state():
    doc = frappe.get_single("Investment License")
    state = evaluate(doc.get("license_key") or "")
    if state.status != doc.status or state.tier != doc.tier:
        _persist(doc, state)
    return state


def has_feature(feature):
    return current_state().allows(feature)


def require_feature(feature):
    if not has_feature(feature):
        frappe.throw(
            f"{feature.title()} requires the Pro tier. Enter a valid license in Investment License.",
            frappe.PermissionError,
        )


def save_license(license_key):
    frappe.only_for(("Investment Manager", "System Manager"))
    doc = frappe.get_single("Investment License")
    state = evaluate(license_key or "")
    if state.status == "invalid":
        frappe.throw("This license key is invalid for Frappe Investing.")
    doc.license_key = license_key or ""
    _persist(doc, state, force=True)
    return {"tier": doc.tier, "status": doc.status, "customer": doc.customer, "expires": doc.expires}


def _persist(doc, state, force=False):
    doc.tier = state.tier
    doc.status = state.status
    doc.customer = state.customer
    doc.expires = state.expires or None
    doc.validated_at = now_datetime()
    doc.status_note = {
        "none": "No license entered. Standard features are free forever.",
        "active": "License verified offline.",
        "expired": "License expired. Pro features are disabled; renew to restore them.",
        "invalid": "License rejected.",
    }[state.status]
    frappe.flags.investing_internal = True
    try:
        doc.save(ignore_permissions=True)
    finally:
        frappe.flags.investing_internal = False


def check_expiry():
    """Daily scheduler: reflect expiry without touching history."""
    current_state()
