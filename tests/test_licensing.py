import base64
import json
from datetime import date

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from frappe_investing import licensing


@pytest.fixture
def keypair(monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(licensing, "PUBLIC_KEY", base64.urlsafe_b64encode(public).decode())
    return private


def make_license(private, **payload):
    full = {
        "product": "frappe-investing",
        "tier": "pro",
        "customer": "Acme Ltd",
        "issued": "2026-09-16",
        "expires": "2027-09-16",
        **payload,
    }
    return licensing.sign_license(full, private)


def test_pro_license_is_unlimited_by_default(keypair):
    state = licensing.evaluate(make_license(keypair), on=date(2027, 1, 1))
    assert state.status == "active"
    assert state.tier == "pro"
    assert state.max_asset_classes is None
    assert state.max_value is None
    assert state.value_currency is None


def test_payload_limits_override_tier_defaults(keypair):
    state = licensing.evaluate(
        make_license(keypair, max_asset_classes=2, max_value="500000", value_currency="USD"),
        on=date(2027, 1, 1),
    )
    assert state.max_asset_classes == 2
    assert state.max_value == "500000"
    assert state.value_currency == "USD"


def test_value_cap_without_currency_is_invalid(keypair):
    state = licensing.evaluate(make_license(keypair, max_value="1000"), on=date(2027, 1, 1))
    assert state.status == "invalid"


def test_bad_class_count_is_invalid(keypair):
    for bad in (0, -3, "three", 2.5):
        state = licensing.evaluate(make_license(keypair, max_asset_classes=bad), on=date(2027, 1, 1))
        assert state.status == "invalid", bad


def test_unlimited_classes_via_null(keypair):
    state = licensing.evaluate(make_license(keypair, max_asset_classes=None), on=date(2027, 1, 1))
    assert state.status == "active"
    assert state.max_asset_classes is None


def test_tampered_payload_is_rejected(keypair):
    license_key = make_license(keypair)
    parts = license_key.split(".")
    payload = json.loads(licensing._b64decode(parts[1]))
    payload["max_asset_classes"] = 99
    parts[1] = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    tampered = ".".join(parts)
    state = licensing.evaluate(tampered, on=date(2027, 1, 1))
    assert state.status == "invalid"
    assert state.max_asset_classes == 1  # falls back to the free limit


def test_expired_license_falls_back_to_free_limits(keypair):
    state = licensing.evaluate(
        make_license(keypair, expires="2026-12-31", max_asset_classes=8), on=date(2027, 2, 1)
    )
    assert state.status == "expired"
    assert state.tier == "standard"
    assert state.max_asset_classes == 1
    assert state.max_value is None


def test_wrong_product_is_invalid(keypair):
    state = licensing.evaluate(make_license(keypair, product="other-app"), on=date(2027, 1, 1))
    assert state.status == "invalid"


def test_empty_license_is_free_tier_not_error():
    state = licensing.evaluate("", on=date(2027, 1, 1))
    assert state.tier == "standard" and state.status == "none"
    assert state.max_asset_classes == 1
    assert state.max_value is None


def test_malformed_license_is_invalid_not_crash(keypair):
    for bad in ("not-a-license", "FINV1..", "FINV1.!!!.@@@"):
        assert licensing.evaluate(bad, on=date(2027, 1, 1)).status == "invalid"
