"""Offline license verification for paid tiers.

License format: FINV1.<base64url(payload)>.<base64url(ed25519 signature)>
Payload is canonical JSON; the signature covers exactly those bytes.
Open source note: this is a commercial control, not DRM — anyone can edit the source.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PRODUCT = "frappe-investing"
PREFIX = "FINV1"

# Dev/test keypair. The publisher's private key never ships; generate production keys
# with scripts/make_license.py and replace this public key at release time.
PUBLIC_KEY = ""
_TIER_FEATURES = {"standard": frozenset(), "pro": frozenset({"crypto"})}


@dataclass(frozen=True)
class LicenseState:
    status: str  # none | active | expired | invalid
    tier: str  # standard | pro
    customer: str = ""
    expires: str = ""

    def allows(self, feature):
        gated = frozenset().union(*_TIER_FEATURES.values())
        if feature not in gated:
            return True  # ungated capability (stocks, bonds, brokers…) is always on
        return self.status == "active" and feature in _TIER_FEATURES.get(self.tier, frozenset())


def _b64decode(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_license(payload, private_key):
    """Publisher-side signing. Never called by the installed app."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = private_key.sign(body)
    return ".".join(
        [
            PREFIX,
            base64.urlsafe_b64encode(body).decode().rstrip("="),
            base64.urlsafe_b64encode(signature).decode().rstrip("="),
        ]
    )


def evaluate(license_key, *, on=None):
    on = on or date.today()
    if not license_key or not str(license_key).strip():
        return LicenseState(status="none", tier="standard")
    try:
        prefix, payload_b64, sig_b64 = str(license_key).strip().split(".")
        if prefix != PREFIX:
            raise ValueError("bad prefix")
        body = _b64decode(payload_b64)
        payload = json.loads(body)
        signature = _b64decode(sig_b64)
        public = Ed25519PublicKey.from_public_bytes(_b64decode(PUBLIC_KEY))
        public.verify(signature, body)
        if payload.get("product") != PRODUCT or payload.get("tier") not in _TIER_FEATURES:
            raise ValueError("product/tier mismatch")
        expires = date.fromisoformat(payload["expires"])
    except (ValueError, TypeError, InvalidSignature, json.JSONDecodeError, binascii.Error):
        return LicenseState(status="invalid", tier="standard")
    if expires < on:
        return LicenseState(
            status="expired",
            tier="standard",
            customer=payload.get("customer", ""),
            expires=payload["expires"],
        )
    return LicenseState(
        status="active",
        tier=payload["tier"],
        customer=payload.get("customer", ""),
        expires=payload["expires"],
    )
