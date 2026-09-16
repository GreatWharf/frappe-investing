# Tiers and licensing

## What the tiers are

**Standard — free, MIT, forever.** Stocks, ETFs, bonds and funds; every broker connector; CSV import; corporate actions; tax lots; TWR/XIRR; accounting integration.

**Pro — paid.** Adds the Crypto asset class (crypto securities, events, CoinGecko prices).

## How enforcement actually works

Frappe Cloud plan feature lists do not change app behavior, so the app enforces tiers itself:

- A license key is an offline-signed Ed25519 token (`FINV1.<payload>.<signature>`) carrying product, tier, customer and expiry.
- Verification happens locally against the public key embedded in the app; nothing phones home.
- The gate is checked server-side when a Crypto security is saved, when crypto events are validated, and before CoinGecko calls.
- Expired licenses fall back to Standard; existing crypto history is never deleted, but new crypto activity is refused until renewed.

## The honest caveat

This is open-source software. A technically capable user can edit the gate out. The license is a commercial control for legitimate customers, not DRM. The business case is support, maintenance and trust — plus the Marketplace listing's paid plan reflecting the same tiers.

## For the publisher

`scripts/make_license.py` generates the keypair (private key to a 0600 file, never printed) and signs customer licenses. The production public key replaces `licensing.PUBLIC_KEY` at release time. CI must never contain the private key.
