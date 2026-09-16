import base64
from datetime import date

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from frappe_investing import licensing, marketplace


def transport_returning(message):
    def send(url, data, timeout):
        send.calls.append((url, data, timeout))
        return {"message": message}

    send.calls = []
    return send


def transport_raising(exc):
    def send(url, data, timeout):
        raise exc

    return send


# ------------------------------------------------------------- plan names
@pytest.mark.parametrize(
    "plan,tier",
    [
        ("Pro", "pro"),
        ("pro", "pro"),
        ("Professional", "pro"),
        ("Frappe Investing Pro", "pro"),
        ("Investing Pro Monthly", "pro"),
        ("Pro Plan", "pro"),
        ("pro-annual", "pro"),
        ("Free", "standard"),
        ("Investing Free Plan", "standard"),
        ("Trial", "standard"),
    ],
)
def test_published_plan_names_map_to_tiers(plan, tier):
    assert marketplace.plan_tier(plan) == tier


@pytest.mark.parametrize("plan", ["Enterprise", "Starter", "pro max", "Unlimited", "$5"])
def test_an_unknown_plan_name_still_pays_for_everything(plan):
    # One paid plan on the listing, so a name this build has not seen means it
    # was renamed, not that the customer stopped paying.
    assert marketplace.plan_tier(plan) == "pro"


@pytest.mark.parametrize("plan", ["", None, "Investing"])
def test_a_plan_that_normalizes_to_nothing_is_the_free_tier(plan):
    assert marketplace.plan_tier(plan) == "standard"


# ------------------------------------------------------------ fetching
def test_fetch_sends_secret_to_the_developer_endpoint():
    send = transport_returning(
        {"document_name": "frappe_investing", "enabled": 1, "plan": "Pro", "site": "acme.frappe.cloud"}
    )
    info = marketplace.fetch_subscription("sk-secret-40-chars", transport=send)
    url, data, timeout = send.calls[0]
    assert url.endswith("press.api.developer.marketplace.get_subscription_info")
    assert data == {"secret_key": "sk-secret-40-chars"}
    assert timeout == marketplace.TIMEOUT
    assert info == {
        "plan": "Pro",
        "site": "acme.frappe.cloud",
        "enabled": True,
        "document_name": "frappe_investing",
    }


def test_missing_secret_is_unavailable_not_a_crash():
    for secret in ("", "   ", None):
        with pytest.raises(marketplace.SubscriptionUnavailable):
            marketplace.fetch_subscription(secret, transport=transport_returning({}))


def test_network_failure_becomes_subscription_unavailable():
    send = transport_raising(OSError("connection reset"))
    with pytest.raises(marketplace.SubscriptionUnavailable, match="connection reset"):
        marketplace.fetch_subscription("sk", transport=send)


@pytest.mark.parametrize("payload", [{"message": None}, {"message": {"site": "x"}}, {}, "nonsense"])
def test_unexpected_payload_is_unavailable(payload):
    def send(url, data, timeout):
        return payload

    with pytest.raises(marketplace.SubscriptionUnavailable):
        marketplace.fetch_subscription("sk", transport=send)


# ------------------------------------------------------- subscription state
def test_enabled_pro_subscription_grants_the_pro_tier():
    state = marketplace.subscription_state(
        {"plan": "Pro", "site": "acme.frappe.cloud", "enabled": True}
    )
    assert state.status == "active"
    assert state.tier == "pro"
    assert state.source == "cloud"
    assert state.customer == "acme.frappe.cloud"
    assert state.max_asset_classes == licensing.tier_limits("pro")[0]


def test_a_free_plan_is_an_honest_standard_state_not_none():
    # The dashboard needs to name the plan the limits came from, so a live
    # subscription on the free plan is a state rather than a hole.
    state = marketplace.subscription_state({"plan": "Free", "site": "acme.frappe.cloud", "enabled": True})
    assert state.tier == "standard"
    assert state.source == "cloud"
    assert state.max_asset_classes == 1


@pytest.mark.parametrize(
    "info",
    [
        None,
        {"plan": "Pro", "enabled": False},
        {"plan": "", "enabled": True},
    ],
)
def test_no_live_subscription_returns_none(info):
    assert marketplace.subscription_state(info) is None


# ------------------------------------------------------------ precedence
@pytest.fixture
def keypair(monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(licensing, "PUBLIC_KEY", base64.urlsafe_b64encode(public).decode())
    return private


def make_key(private, **payload):
    return licensing.sign_license(
        {
            "product": "frappe-investing",
            "tier": "pro",
            "customer": "Acme Ltd",
            "issued": "2026-09-16",
            "expires": "2027-09-16",
            **payload,
        },
        private,
    )


def test_cloud_plan_alone_grants_its_tier():
    cloud = marketplace.subscription_state({"plan": "Pro", "enabled": True})
    merged = licensing.most_generous(cloud, licensing.evaluate(""))
    assert merged.source == "cloud"
    assert merged.tier == "pro"


def test_a_bigger_key_is_not_demoted_by_a_smaller_cloud_plan(keypair):
    cloud = marketplace.subscription_state({"plan": "Free", "enabled": True})
    key = licensing.evaluate(make_key(keypair, max_asset_classes=None), on=date(2027, 1, 1))
    merged = licensing.most_generous(cloud, key)
    assert merged.source == "key"
    assert merged.max_asset_classes is None


def test_a_cloud_plan_carries_a_site_whose_key_expired(keypair):
    cloud = marketplace.subscription_state({"plan": "Pro", "enabled": True})
    key = licensing.evaluate(make_key(keypair, expires="2026-01-01"), on=date(2027, 1, 1))
    assert key.status == "expired"
    merged = licensing.most_generous(cloud, key)
    assert merged.status == "active"
    assert merged.tier == "pro"


def test_an_invalid_key_does_not_block_a_cloud_subscriber():
    cloud = marketplace.subscription_state({"plan": "Pro", "enabled": True})
    merged = licensing.most_generous(cloud, licensing.evaluate("not-a-license"))
    assert merged.tier == "pro"
    assert merged.source == "cloud"


def test_no_cloud_and_no_key_is_the_free_tier():
    merged = licensing.most_generous(None, licensing.evaluate(""))
    assert merged.status == "none"
    assert merged.tier == "standard"
    assert merged.max_asset_classes == 1


def test_expired_key_survives_the_merge_so_the_banner_can_explain_it(keypair):
    key = licensing.evaluate(make_key(keypair, expires="2026-01-01"), on=date(2027, 1, 1))
    merged = licensing.most_generous(None, key)
    assert merged.status == "expired"
    assert merged.expires == "2026-01-01"


def test_value_caps_rank_below_uncapped(keypair):
    capped = licensing.evaluate(
        make_key(keypair, max_value="500000", value_currency="USD"), on=date(2027, 1, 1)
    )
    uncapped = marketplace.subscription_state({"plan": "Pro", "enabled": True})
    assert licensing.most_generous(uncapped, capped).max_value is None


def test_a_non_decimal_value_cap_is_rejected(keypair):
    state = licensing.evaluate(
        make_key(keypair, max_value="lots", value_currency="USD"), on=date(2027, 1, 1)
    )
    assert state.status == "invalid"
