"""Marketplace tier-name tests: known-free names stay free, paid stays fail-open.

Item 7: an unknown plan name grants the paid tier on purpose (a renamed paid
plan must never downgrade a paying customer). This pins the other side: every
known free-plan name — including multi-word variants the listing might use —
must keep mapping to the free tier, so fail-open never leaks the paid tier
to a genuinely free subscription.
"""

import pytest

from frappe_investing import marketplace


@pytest.mark.parametrize(
    "plan",
    [
        "Free",
        "free",
        "FREE",
        "Trial",
        "trial",
        "TRIAL",
        "",
        None,
        "Investing",
        "Frappe Investing",
        "Investing Free",
        "Investing Free Plan",
        "Frappe Investing Free Monthly",
        "Free Plan",
        "Free Monthly",
        "Free Annual",
        "Trial Plan",
        "Investing Trial",
        "Investing Trial Monthly",
        "free plan",
        "free-trial",
        "trial-annual",
    ],
)
def test_known_free_names_stay_on_the_free_tier(plan):
    assert marketplace.plan_tier(plan) == "standard"


@pytest.mark.parametrize(
    "plan",
    [
        "Pro",
        "pro",
        "Professional",
        "Frappe Investing Pro Monthly",
        "Investing $5",
        "Enterprise",
        "Starter",
        "pro max",
        "Unlimited",
        "$5",
    ],
)
def test_paid_and_unknown_names_grant_the_paid_tier(plan):
    # Fail-open for paying customers: a renamed paid plan must not downgrade.
    assert marketplace.plan_tier(plan) == "pro"


def test_free_trial_combination_does_not_fail_open():
    """'Free Trial' names the free tier twice — it must not grant paid."""
    assert marketplace.plan_tier("Free Trial") == "standard"
    assert marketplace.plan_tier("Trial Free") == "standard"


@pytest.mark.parametrize(
    "plan",
    [
        "Free Trial Monthly",
        "Trial Free Annual",
        "Free ACME Corp Add-on",
        "Frappe Investing Free Trial",
        "free trial plan",
    ],
)
def test_any_free_token_keeps_the_free_tier(plan):
    """Any free token after normalization pins the free tier; only genuinely
    unknown names (no free token at all) fail open to paid."""
    assert marketplace.plan_tier(plan) == "standard"
