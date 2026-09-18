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
`PLAN_TIERS` maps the published names onto tiers. The listing has one paid
plan, so anything that is not a named free plan grants it; see FREE_PLANS.
"""

import requests

from . import licensing

# press derives this from the Marketplace App record's name, so it tracks the
# app name rather than anything we choose here.
CONFIG_KEY = "sk_frappe_investing"
DEFAULT_BASE_URL = "https://frappecloud.com"
API_PATH = "/api/method/press.api.developer.marketplace.get_subscription_info"
TIMEOUT = 10

# The listing carries a single paid plan, so any enabled subscription that is
# not on a named free plan grants everything. That fails toward the paying
# customer on purpose: renaming the plan on the listing must never downgrade
# someone who is being billed, and the alternative failure — a free-plan site
# getting the paid tier — costs one subscription rather than a refund and a
# lost customer. Introducing a cheaper paid tier means naming it here first.
FREE_PLANS = {"", "free", "trial"}
# Token sets that are free in any combination: "Free Trial", "Trial Free" and
# "free-trial" name the free tier twice and must never fail open to paid.
FREE_TOKENS = {"free", "trial"}
PLAN_TIERS = {
    "standard": "pro",
    "pro": "pro",
    "professional": "pro",
}
PAID_DEFAULT = "pro"

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
    """The tier a published plan name maps to. Unknown paid names get PAID_DEFAULT."""
    name = normalize_plan(plan)
    if not name:
        return "standard"
    tokens = set(name.split())
    if tokens <= FREE_TOKENS:
        # Every word names the free tier ("Free", "Trial", "Free Trial",
        # "free-trial"): never fail open to paid.
        return "standard"
    if tokens & FREE_TOKENS:
        # A free word inside a longer name ("Free ACME Corp Add-on") still
        # names a free-tier subscription; only genuinely unknown names
        # (no free token at all) fail open to paid.
        return "standard"
    return PLAN_TIERS.get(name, PAID_DEFAULT)


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
    """A LicenseState for an active subscription, or None when there is none.

    None means no subscription at all, or a disabled one, and leaves the
    caller's fallback (the license key, else the free tier) in charge. A live
    subscription on a free plan is not None: it is an honest `standard` state,
    so the dashboard can name the plan the limits came from.
    """
    if not info or not info.get("enabled") or not info.get("plan"):
        return None
    tier = plan_tier(info["plan"])
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
