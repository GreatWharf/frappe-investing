"""License storage and tier enforcement at the Frappe boundary.

Enforcement model: creating a Security in an asset class beyond the licensed
count fails closed (a clear PermissionError). Anything already recorded —
existing securities, prices, valuations, snapshots — stays visible; the
dashboard shows a usage banner instead of locking the user out of their data.
"""

import frappe
from frappe.utils import now_datetime

from .core.money import dec
from .licensing import evaluate

STANDARD_LIMITS = {"asset_classes": 1, "max_value": None}


def current_state():
    doc = frappe.get_single("Investment License")
    state = evaluate(doc.get("license_key") or "")
    if state.status != doc.status or state.tier != doc.tier:
        _persist(doc, state)
    return state


def public_state():
    """Dashboard-safe view of the current license."""
    state = current_state()
    return {
        "tier": state.tier,
        "status": state.status,
        "customer": state.customer,
        "expires": state.expires,
        "max_asset_classes": state.max_asset_classes,
        "max_value": state.max_value,
        "value_currency": state.value_currency,
    }


def used_asset_classes():
    """Classes the user actually tracks; Benchmark rows are catalog, not holdings."""
    return sorted(
        set(
            frappe.get_all(
                "Security",
                filters={"status": "Active", "asset_class": ["!=", "Benchmark"]},
                pluck="asset_class",
                distinct=True,
            )
        )
    )


def _limit_label(state):
    return (
        f"{state.max_asset_classes} asset class{'es' if state.max_asset_classes != 1 else ''}"
        if state.max_asset_classes is not None
        else "unlimited asset classes"
    )


def require_asset_class(asset_class):
    """Fail closed when a new Security would exceed the licensed class count."""
    state = current_state()
    if state.max_asset_classes is None:
        return
    used = used_asset_classes()
    if asset_class in used or len(used) < state.max_asset_classes:
        return
    frappe.throw(
        f"Your license covers {_limit_label(state)}; {', '.join(used) or 'none'} "
        f"{'is' if len(used) == 1 else 'are'} already in use. "
        f"Upgrade in Investment License to track {asset_class}.",
        frappe.PermissionError,
    )


def portfolio_value_check(total_value, base_currency):
    """Advisory: compare a portfolio value against the license's value cap.

    Returns None when within limits, or a dict describing the breach. A missing
    FX rate fails closed (breach with reason "missing_fx") rather than ignoring
    the cap.
    """
    state = current_state()
    if state.max_value is None:
        return None
    cap = dec(state.max_value)
    value = dec(total_value)
    if base_currency == state.value_currency:
        converted, rate_note = value, None
    else:
        row = frappe.get_all(
            "FX Rate",
            filters={
                "from_currency": base_currency,
                "to_currency": state.value_currency,
            },
            fields=["rate"],
            order_by="date desc",
            limit_page_length=1,
        )
        if not row:
            return {
                "breached": True,
                "reason": "missing_fx",
                "base_currency": base_currency,
                "value_currency": state.value_currency,
                "max_value": state.max_value,
            }
        converted, rate_note = value * dec(row[0].rate), row[0].rate
    if converted <= cap:
        return None
    return {
        "breached": True,
        "reason": "over_cap",
        "converted_value": str(converted),
        "max_value": state.max_value,
        "value_currency": state.value_currency,
        "base_currency": base_currency,
        "rate_used": str(rate_note) if rate_note is not None else "1",
    }


def _limits_text(state):
    classes = (
        f"{state.max_asset_classes} asset class{'es' if state.max_asset_classes != 1 else ''}"
        if state.max_asset_classes is not None
        else "unlimited asset classes"
    )
    if state.max_value is None:
        return classes
    return f"{classes}, up to {state.max_value} {state.value_currency} tracked"


def save_license(license_key):
    frappe.only_for(("Investment Manager", "System Manager"))
    doc = frappe.get_single("Investment License")
    state = evaluate(license_key or "")
    if state.status == "invalid":
        frappe.throw("This license key is invalid for Frappe Investing.")
    doc.license_key = license_key or ""
    _persist(doc, state, force=True)
    return public_state()


def _persist(doc, state, force=False):
    doc.tier = state.tier
    doc.status = state.status
    doc.customer = state.customer
    doc.expires = state.expires or None
    doc.limits = _limits_text(state)
    doc.validated_at = now_datetime()
    doc.status_note = {
        "none": "No license entered. The free tier tracks one asset class forever.",
        "active": "License verified offline.",
        "expired": "License expired; the free tier's limits apply. Renew to restore your tier.",
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
