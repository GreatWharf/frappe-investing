"""Frappe Cloud Marketplace subscriptions as the primary tier source.

When a site installs a paid app from the Marketplace, press creates a
Subscription, generates a 40-character secret, and registers the site config
key `sk_<app_name>` for it. That secret lands in the customer's site config,
and press exposes the plan behind it through a guest endpoint:

    press.api.developer.marketplace.get_subscription_info(secret_key)
      -> {"document_name", "enabled", "plan", "site"}

So a Cloud customer never touches a license key: they pick a plan in the
Frappe Cloud dashboard, and the app reads back which one. Sites that are not
on Frappe Cloud have no subscription and fall back to an offline-signed key
(see licensing.py).

What Frappe Cloud does *not* do is enforce anything. A plan's "Features" list
is marketing copy; press will not switch app behaviour on it. This module
turns the plan *name* into a tier, and the app enforces the tier itself.

Plan names are free text from the publisher's Marketplace listing, so
`PLAN_TIERS` maps the published names onto tiers. An unrecognised plan grants
nothing and is reported back by name rather than guessing a tier in either
direction.
"""

import requests

from . import licensing

# press derives this from the Marketplace App record's name, so it tracks the
# app name rather than anything we choose here.
CONFIG_KEY = "sk_frappe_investing"
DEFAULT_BASE_URL = "https://frappecloud.com"
API_PATH = "/api/method/press.api.developer.marketplace.get_subscription_info"
TIMEOUT = 10

# Published Marketplace plan names, normalized, mapped onto tiers. Keep this in
# step with the plans on the listing; renaming a plan there without adding it
# here downgrades paying sites to the free limits.
PLAN_TIERS = {
    "free": "standard",
    "standard": "standard",
    "starter": "standard",
    "pro": "pro",
    "professional": "pro",
}

# Words stripped off a plan name before lookup, so "Frappe Investing Pro Monthly"
# and "Pro" reach the same tier.
_LEADING = {"frappe", "investing"}
_TRAILING = {"plan", "monthly", "month", "annual", "annually", "yearly", "year"}


class SubscriptionUnavailable(Exception):
    """Frappe Cloud could not be reached, or refused the secret."""


def normalize_plan(plan):
    tokens = str(plan or "").lower().replace("-", " ").replace("/", " ").split()
    while tokens and tokens[0] in _LEADING:
        tokens.pop(0)
    while tokens and tokens[-1] in _TRAILING:
        tokens.pop()
    return " ".join(tokens)


def plan_tier(plan):
    """The tier a published plan name maps to, or None when it is unrecognised."""
    return PLAN_TIERS.get(normalize_plan(plan))


def fetch_subscription(secret_key, *, base_url=DEFAULT_BASE_URL, timeout=TIMEOUT, transport=None):
    """Read this site's subscription from press. Raises SubscriptionUnavailable."""
    secret = str(secret_key or "").strip()
    if not secret:
        raise SubscriptionUnavailable("No Frappe Cloud subscription key in this site's config.")
    send = transport or _post
    try:
        payload = send(base_url.rstrip("/") + API_PATH, {"secret_key": secret}, timeout)
    except SubscriptionUnavailable:
        raise
    except Exception as exc:
        raise SubscriptionUnavailable(f"Could not reach Frappe Cloud: {exc}") from exc
    info = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(info, dict) or "plan" not in info:
        raise SubscriptionUnavailable("Frappe Cloud returned no subscription for this site.")
    return {
        "plan": info.get("plan") or "",
        "site": info.get("site") or "",
        "enabled": bool(info.get("enabled")),
        "document_name": info.get("document_name") or "",
    }


def _post(url, data, timeout):
    response = requests.post(url, json=data, timeout=timeout)
    response.raise_for_status()
    return response.json()


def subscription_state(info):
    """A LicenseState for an active subscription, or None when it grants nothing.

    None covers every "no paid tier here" case — no subscription, a disabled
    one, or a plan name this build does not recognise — and leaves the caller's
    fallback (the license key, else the free tier) in charge.
    """
    if not info or not info.get("enabled"):
        return None
    tier = plan_tier(info.get("plan"))
    if tier is None:
        return None
    classes, value, currency = licensing.tier_limits(tier)
    return licensing.LicenseState(
        status="active",
        tier=tier,
        customer=info.get("site") or "",
        max_asset_classes=classes,
        max_value=value,
        value_currency=currency,
        source="cloud",
    )
