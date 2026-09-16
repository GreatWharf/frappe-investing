# Tiers and licensing

## What the tiers are

Tiers are defined by **how much you track**, not which features you unlock. Every tier
includes every feature: all broker connectors, CSV import, corporate actions, tax lots,
TWR/XIRR, Sharpe, benchmarks, and the accounting integration. What changes with the tier
is scope:

- **Asset classes** — how many of the five asset classes (Stock, ETF, Bond, Fund, Crypto)
  you may track at once. The free tier covers **1**; Pro covers **5**; custom licenses can
  carry any count, or none (unlimited).
- **Portfolio value cap (optional)** — a license may cap the tracked portfolio value, stated
  in a reference currency (e.g. $500,000 USD). Valuations convert through the FX RateBook
  to compare, exactly like portfolio reporting does.

The free tier is 1 asset class with no value cap, forever, MIT-licensed.

## Where a tier comes from

Two sources, and you only ever deal with one of them.

### On Frappe Cloud: your plan, nothing to install

Subscribe on the Marketplace listing and you are done. Press writes the subscription secret
into your site config as `sk_frappe_investing`, and the app reads the plan back from
`press.api.developer.marketplace.get_subscription_info`, which returns `document_name`,
`enabled`, `plan` and `site`. Change your plan in the Frappe Cloud dashboard and the app
follows; there is no key to paste, request, or renew.

What Frappe Cloud does **not** do is enforce anything. A plan's "Features" list is marketing
copy, and press will not switch app behaviour on it (the official docs are explicit: "It is
not possible to do feature isolation based on paid or free App Plans"). So the app reads the
plan *name* and applies the tier itself. Plan names map to tiers in
`marketplace.PLAN_TIERS`; renaming a plan on the listing without adding it there would
downgrade paying sites, and the dashboard names any plan it does not recognise instead of
guessing a tier in either direction.

### Everywhere else: an offline-signed key

Self-hosted sites have no subscription, so a paid tier arrives as an Ed25519 token
(`FINV1.<payload>.<signature>`) carrying product, tier, customer, expiry, and the tier's
limits. Payload limits override the tier's built-in defaults, so a custom deal needs no code
change. Verification is local, against the public key embedded in the app; nothing phones home.

### When both exist

Whichever grants more wins. A Cloud plan never demotes a customer holding a larger key, and
an old key never holds back a big Cloud plan. Ties go to the Cloud plan, since that is the
one that refreshes itself.

## How enforcement actually works

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

## Why the Cloud plan is cached

Resolving a tier runs on every Security insert, so it must never make a network call. The
plan is read on a schedule (daily, plus the dashboard's **Refresh Plan** button) and cached
on the Investment License single with the time it was read.

Only a successful read moves that timestamp, because it is what the grace window measures:
while Frappe Cloud is unreachable the cached plan keeps working for **7 days**
(`license_service.CLOUD_GRACE`), so a press outage cannot downgrade a paying site. Past the
grace window the site falls back to its key, else the free tier.

## The honest caveat

This is open-source software. A technically capable user can edit the gate out. The license
is a commercial control for legitimate customers, not DRM. The business case is support,
maintenance and trust.

## For the publisher

Name the Marketplace plans so they normalize onto `marketplace.PLAN_TIERS`: leading "Frappe"
and "Investing" and trailing "Plan", "Monthly", "Annual" and "Yearly" are stripped before
lookup, so "Frappe Investing Pro Monthly", "Pro Plan" and "Pro" all reach the `pro` tier.
Adding a plan means adding its normalized name to that map.

`scripts/make_license.py` covers the non-Cloud side: it generates the keypair (private key to
a 0600 file, never printed) and signs customer licenses. Tier presets (`standard` = 1 class,
`pro` = 5) can be overridden per customer with `--max-asset-classes N --max-value V
--value-currency CCC`. The production public key replaces `licensing.PUBLIC_KEY` at release
time. CI must never contain the private key.
