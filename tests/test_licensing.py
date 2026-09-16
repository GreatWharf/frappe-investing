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


def test_valid_pro_license_unlocks_crypto(keypair):
    state = licensing.evaluate(make_license(keypair), on=date(2027, 1, 1))
    assert state.tier == "pro"
    assert state.allows("crypto")
    assert state.status == "active"


def test_standard_tier_has_no_crypto(keypair):
    state = licensing.evaluate(make_license(keypair, tier="standard"), on=date(2027, 1, 1))
    assert state.tier == "standard"
    assert not state.allows("crypto")


def test_tampered_payload_is_rejected(keypair):
    license_key = make_license(keypair)
    parts = license_key.split(".")
    payload = json.loads(licensing._b64decode(parts[1]))
    payload["tier"] = "pro-lifetime"
    parts[1] = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    tampered = ".".join(parts)
    state = licensing.evaluate(tampered, on=date(2027, 1, 1))
    assert state.status == "invalid"
    assert not state.allows("crypto")


def test_expired_license_stays_standard_with_grace_flag(keypair):
    state = licensing.evaluate(make_license(keypair, expires="2026-12-31"), on=date(2027, 2, 1))
    assert state.status == "expired"
    assert state.tier == "standard"
    assert not state.allows("crypto")


def test_wrong_product_is_invalid(keypair):
    state = licensing.evaluate(make_license(keypair, product="other-app"), on=date(2027, 1, 1))
    assert state.status == "invalid"


def test_empty_license_is_standard_not_error():
    state = licensing.evaluate("", on=date(2027, 1, 1))
    assert state.tier == "standard" and state.status == "none"
    assert state.allows("stocks")


def test_malformed_license_is_invalid_not_crash(keypair):
    for bad in ("not-a-license", "FINV1..", "FINV1.!!!.@@@"):
        assert licensing.evaluate(bad, on=date(2027, 1, 1)).status == "invalid"
