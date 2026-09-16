"""License storage and tier enforcement at the Frappe boundary.

Two tier sources feed one state. A Frappe Cloud Marketplace install reads its
plan from the subscription (marketplace.py); anywhere else, an offline-signed
key does the job (licensing.py). Whichever grants more wins, so neither can
demote the other.

Resolving a tier never touches the network: enforcement runs on every Security
insert, so the Cloud plan is read on a schedule and cached on the Investment
License single. A failed read keeps the cached plan for CLOUD_GRACE, because a
press outage must not downgrade a paying site.

Enforcement model: creating a Security in an asset class beyond the licensed
count fails closed (a clear PermissionError). Anything already recorded —
existing securities, prices, valuations, snapshots — stays visible; the
dashboard shows a usage banner instead of locking the user out of their data.
"""

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from . import marketplace
from .core.money import dec
from .licensing import evaluate, most_generous

STANDARD_LIMITS = {"asset_classes": 1, "max_value": None}

# How long a cached Cloud plan keeps working while press is unreachable.
CLOUD_GRACE = timedelta(days=7)


def _cloud_secret():
    return frappe.conf.get(marketplace.CONFIG_KEY)


def _cached_cloud_state(doc):
    """The last plan read from Frappe Cloud, while it is still inside the grace window."""
    checked = doc.get("cloud_checked_at")
    if not doc.get("cloud_plan") or not checked:
        return None
    if now_datetime() - get_datetime(checked) > CLOUD_GRACE:
        return None
    return marketplace.subscription_state(
        {"plan": doc.cloud_plan, "site": doc.get("cloud_site") or "", "enabled": True}
    )


def _resolve(doc):
    """Merge the cached Cloud plan with the stored key, persisting a changed verdict."""
    state = most_generous(_cached_cloud_state(doc), evaluate(doc.get("license_key") or ""))
    if (state.status, state.tier, state.source) != (doc.status, doc.tier, doc.get("source") or ""):
        _persist(doc, state)
    return state


def current_state():
    return _resolve(frappe.get_single("Investment License"))


def public_state():
    """Dashboard-safe view of the current license."""
    doc = frappe.get_single("Investment License")
    state = _resolve(doc)
    return {
        "tier": state.tier,
        "status": state.status,
        "source": state.source,
        "customer": state.customer,
        "expires": state.expires,
        "max_asset_classes": state.max_asset_classes,
        "max_value": state.max_value,
        "value_currency": state.value_currency,
        "cloud_managed": bool(_cloud_secret()),
        "cloud_plan": doc.get("cloud_plan") or "",
        "cloud_site": doc.get("cloud_site") or "",
        "cloud_note": doc.get("cloud_note") or "",
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
    _persist(doc, most_generous(_cached_cloud_state(doc), state))
    return public_state()


def refresh_cloud_subscription():
    """Re-read the plan from Frappe Cloud. Daily scheduler, and the Refresh button.

    Only a successful read moves `cloud_checked_at`, since that timestamp is
    what the grace window measures: while press is down the cached plan stands,
    and once the grace window lapses the site falls back to its key.
    """
    doc = frappe.get_single("Investment License")
    secret = _cloud_secret()
    if not secret:
        _set_cloud(doc, "", "", "This site is not a Frappe Cloud Marketplace install.", read=False)
    else:
        try:
            info = marketplace.fetch_subscription(secret)
        except marketplace.SubscriptionUnavailable as exc:
            _set_cloud(doc, doc.get("cloud_plan") or "", doc.get("cloud_site") or "", str(exc), read=False)
        else:
            _set_cloud(doc, *_read_plan(info), read=True)
    _persist(doc, most_generous(_cached_cloud_state(doc), evaluate(doc.get("license_key") or "")))
    return public_state()


def _read_plan(info):
    """(plan, site, note) to store for a subscription press just told us about."""
    plan, site = info.get("plan") or "", info.get("site") or ""
    if not info.get("enabled"):
        return "", site, "Your Frappe Cloud subscription is not active."
    if not plan:
        return "", site, "Frappe Cloud reported a subscription with no plan."
    return plan, site, f"Plan '{plan}' read from your Frappe Cloud subscription."


def _set_cloud(doc, plan, site, note, *, read):
    doc.cloud_plan = plan
    doc.cloud_site = site
    doc.cloud_note = note
    if read:
        doc.cloud_checked_at = now_datetime()


def _status_note(state):
    if state.status == "active" and state.source == "cloud":
        return "Tier read from your Frappe Cloud subscription; no license key needed."
    return {
        "none": "No license entered. The free tier tracks one asset class forever.",
        "active": "License key verified offline.",
        "expired": "License expired; the free tier's limits apply. Renew to restore your tier.",
        "invalid": "License rejected.",
    }[state.status]


def _persist(doc, state):
    doc.tier = state.tier
    doc.status = state.status
    doc.source = state.source
    doc.customer = state.customer
    doc.expires = state.expires or None
    doc.limits = _limits_text(state)
    doc.validated_at = now_datetime()
    doc.status_note = _status_note(state)
    frappe.flags.investing_internal = True
    try:
        doc.save(ignore_permissions=True)
    finally:
        frappe.flags.investing_internal = False


def check_expiry():
    """Daily scheduler: reflect expiry without touching history."""
    current_state()
