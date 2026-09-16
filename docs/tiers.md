# Tiers and licensing

## What the tiers are

There are two: **Free** and **Pro**. There is one paid plan, and it covers everything.

Tiers are defined by **how much you track**, not which features you unlock. Both tiers
include every feature: all broker connectors, CSV import, corporate actions, tax lots,
TWR/XIRR, Sharpe, benchmarks, crypto, and the accounting integration. What changes is scope:

- **Asset classes** — how many of Stock, ETF, Bond, Fund and Crypto you may track at once.
  Free covers **1**; Pro is **unlimited**. Pro's limit is stated as unlimited rather than as
  today's count of five, so adding a sixth asset class later cannot retroactively put a
  paying customer over their limit.
- **Portfolio value cap (optional)** — a custom license may cap the tracked portfolio value,
  stated in a reference currency (e.g. $500,000 USD). Valuations convert through the FX
  RateBook to compare, exactly like portfolio reporting does. Neither published tier sets one.

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
plan *name* and applies the tier itself.

Because there is only one paid plan, that mapping fails toward the paying customer: a plan
name that is not in `marketplace.FREE_PLANS` grants Pro, whether or not this build has seen
the name before. Renaming the plan on the listing therefore cannot downgrade someone who is
being billed — and the opposite mistake, a free-plan site getting Pro, costs one subscription
rather than a refund and a lost customer. Introducing a cheaper paid tier means naming it in
`marketplace.PLAN_TIERS` first.

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

The listing carries one paid plan. Name it anything — leading "Frappe" and "Investing" and
trailing "Plan", "Monthly", "Annual" and "Yearly" are stripped before lookup, so "Frappe
Investing Pro Monthly", "Pro Plan" and "Pro" all reach the `pro` tier, and an unlisted name
reaches it too. A **free** plan is the one thing that must be named exactly: its normalized
name has to be in `marketplace.FREE_PLANS` (`free` or `trial`), or its subscribers get Pro.

Crypto ships in the paid tier for now. Splitting it into a separately-priced add-on is a
later change, and it needs a second plan name in `PLAN_TIERS` plus per-class enforcement —
not a code deletion.

`scripts/make_license.py` covers the non-Cloud side: it generates the keypair (private key to
a 0600 file, never printed) and signs customer licenses. Tier presets (`standard` = 1 class,
`pro` = unlimited) can be overridden per customer with `--max-asset-classes N --max-value V
--value-currency CCC`. The production public key replaces `licensing.PUBLIC_KEY` at release
time. CI must never contain the private key.
