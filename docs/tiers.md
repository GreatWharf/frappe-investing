# Tiers and licensing

## What the tiers are

Tiers are defined by **how much you track**, not which features you unlock. Every tier
includes every feature: all broker connectors, CSV import, corporate actions, tax lots,
TWR/XIRR, and the accounting integration. What changes with the tier is scope:

- **Asset classes** — how many of the five asset classes (Stock, ETF, Bond, Fund, Crypto)
  you may track at once. The free tier covers **1**; Pro covers **5**; custom licenses can
  carry any count, or none (unlimited).
- **Portfolio value cap (optional)** — a license may cap the tracked portfolio value, stated
  in a reference currency (e.g. $500,000 USD). Valuations convert through the FX RateBook
  to compare, exactly like portfolio reporting does.

The free tier is 1 asset class with no value cap, forever, MIT-licensed.

## How enforcement actually works

Frappe Cloud plan feature lists do not change app behavior, so the app enforces tiers itself:

- A license key is an offline-signed Ed25519 token (`FINV1.<payload>.<signature>`) carrying
  product, tier, customer, expiry, and the tier's limits (max asset classes, optional value
  cap and its reference currency). Payload limits can override the tier's built-in defaults,
  so a custom deal needs no code change.
- Verification happens locally against the public key embedded in the app; nothing phones home.
- **Creating** a Security in an asset class beyond the licensed count fails closed with a
  clear error — this includes securities auto-created during broker sync (a synced crypto
  position on a 1-class license is refused rather than silently imported).
- **Everything already recorded stays visible.** Existing securities, prices, events,
  valuations, and snapshots are never locked or deleted. When usage exceeds the license —
  too many classes, a portfolio value over the cap, or an expired key — the dashboard shows
  a banner; recording new out-of-tier securities is what stops.
- The value cap compares in the license's reference currency; if the FX rate needed for the
  comparison is missing, the app says so instead of pretending the cap cannot be breached
  (fail closed, but advisory).
- Expired licenses fall back to the free tier's limits (1 class, no cap); history is never
  touched.

## The honest caveat

This is open-source software. A technically capable user can edit the gate out. The license
is a commercial control for legitimate customers, not DRM. The business case is support,
maintenance and trust — plus the Marketplace listing's paid plan reflecting the same tiers.

## For the publisher

`scripts/make_license.py` generates the keypair (private key to a 0600 file, never printed)
and signs customer licenses. Tier presets (`standard` = 1 class, `pro` = 5) can be overridden
per customer with `--max-asset-classes N --max-value V --value-currency CCC`. The production
public key replaces `licensing.PUBLIC_KEY` at release time. CI must never contain the private key.
