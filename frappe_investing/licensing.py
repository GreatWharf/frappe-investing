"""Offline license verification for paid tiers.

License format: FINV1.<base64url(payload)>.<base64url(ed25519 signature)>
Payload is canonical JSON; the signature covers exactly those bytes.
Open source note: this is a commercial control, not DRM — anyone can edit the source.

Tiers are defined by how much they track: a number of asset classes, and an
optional portfolio-value cap in a named reference currency. `None` means
unlimited. The free tier is 1 asset class with no value cap.
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

# (max_asset_classes, max_value, value_currency); None means no limit.
_TIER_LIMITS = {
    "standard": (1, None, None),
    "pro": (5, None, None),
}


@dataclass(frozen=True)
class LicenseState:
    status: str  # none | active | expired | invalid
    tier: str  # standard | pro
    customer: str = ""
    expires: str = ""
    max_asset_classes: int | None = 1
    max_value: str | None = None  # decimal string; None = no cap
    value_currency: str | None = None  # reference currency for max_value

    @property
    def active(self):
        return self.status == "active"


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


def _limits_for(tier, payload):
    """Payload limits override the tier default; absent keys fall back to the tier."""
    default_classes, default_value, default_currency = _TIER_LIMITS[tier]
    classes = payload.get("max_asset_classes", default_classes)
    value = payload.get("max_value", default_value)
    currency = payload.get("value_currency", default_currency)
    if classes is not None and (not isinstance(classes, int) or classes < 1):
        raise ValueError("max_asset_classes must be a positive integer or null")
    if value is not None and not currency:
        raise ValueError("a value cap requires a value_currency")
    return classes, value, currency


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
        if payload.get("product") != PRODUCT or payload.get("tier") not in _TIER_LIMITS:
            raise ValueError("product/tier mismatch")
        classes, value, currency = _limits_for(payload["tier"], payload)
        expires = date.fromisoformat(payload["expires"])
    except (ValueError, TypeError, InvalidSignature, json.JSONDecodeError, binascii.Error):
        return LicenseState(status="invalid", tier="standard")
    if expires < on:
        # Expired licenses fall back to the free tier's limits, not the paid ones.
        classes, value, currency = _TIER_LIMITS["standard"]
        return LicenseState(
            status="expired",
            tier="standard",
            customer=payload.get("customer", ""),
            expires=payload["expires"],
            max_asset_classes=classes,
            max_value=value,
            value_currency=currency,
        )
    return LicenseState(
        status="active",
        tier=payload["tier"],
        customer=payload.get("customer", ""),
        expires=payload["expires"],
        max_asset_classes=classes,
        max_value=value,
        value_currency=currency,
    )
