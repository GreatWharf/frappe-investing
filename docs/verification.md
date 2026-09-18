# Verification record — 2026-09-18

## Executed locally (development machine)

| Check | Result |
|---|---|
| Offline pytest suite (core engine, lots, corporate actions, income, bonds, FX, valuation, TWR/XIRR/Sharpe, benchmarks, accounting mapper, licensing, Marketplace subscriptions, services orchestration, connectors, market data, schema) | **345 passed** |
| Dashboard JS suite (`node tests/js/investing.test.js`) | **28 passed** |
| Ruff lint (`target-version = py310`) | Passed |
| Install preflight — hook and patch targets resolve, 19 doctypes carry a valid module/folder/controller class, fields link only to doctypes that exist, dashboard calls only whitelisted API methods, hooked assets present (`scripts/preflight.py`) | Clean |
| Connector/market-data HTTP error sanitization (secrets never in error strings) | Tested with mocked transports |
| Distribution wheel + source archive contents | Checked by `scripts/check_dist.py` |

## Executed in CI (GitHub Actions, every push)

- `bench-integration.yml`: fresh bench install, then the 7-test integration module
  (`frappe_investing.tests.test_integration`) against real MariaDB on
  **Frappe/ERPNext v15 (Python 3.10, MariaDB 10.6)** and **v16 (Python 3.14, MariaDB 11.8)** — green.
- `image.yml`: production container image build — green.

## Executed on a live deployed site

A Dokploy-hosted test instance running ERPNext v16 plus this app from the production
container image, exercised entirely over HTTP (REST fleet plus a Playwright-driven
browser). No fixtures, no mocks, no shortcuts.

- Every deploy's init step runs migrate + the same 7-test integration module
  in-site: **7/7 passed** on every deploy from 898abc7 onward.
- A 13-scenario end-to-end fleet: **52/52 checks passed**.

| Scenario | What it proves |
|---|---|
| bootstrap, company-bootstrap, accounting-policy | A fresh company gets investing books wired into its own chart of accounts |
| portfolio-setup | Portfolio, investment account and securities created |
| manual-trade (7 checks) | Buy recorded, tax lots opened, journal entry submitted, replay deduped |
| dividend-flow (3) | Dividend posts a submitted JE crediting Dividend Income |
| license-gate (4), license-activate (2) | Free tier blocks a second asset class; an offline-signed pro key unlocks it |
| bond-purchase (5) | Bond buy with the accrued-interest leg; coupon JE credits Interest Income |
| csv-import (8) | Preview resolves securities like posting does, posts, dedupes a re-import |
| coa-sync (3) | Every posted event links a submitted journal entry |
| dashboard (5) | Valuation, performance, connections and accounting status APIs serve the page |
| broker-sync-honesty (5) | A dead connection is refused at enable time, never shows Connected, never logs Success |

### Bugs found only because the fleet ran against a real deployment

1. **License key read-back returned ciphertext.** Password fields must be read with
   `get_password()`; every status call reported "invalid" right after a successful save
   (fixed in 898abc7).
2. **CSV preview disagreed with posting.** Preview never resolved security keys, so a
   row could preview valid and then be refused on post (fixed in 898abc7).
3. **Fresh-site license checks crashed.** `get_password()` throws "Password not found"
   on a never-written field; caught by CI benches, fixed in 68d8c23.
4. **The dashboard page was shadowed by the workspace route, and broker-setup 404'd.**
   v16's router resolves workspaces before pages, and page names must match their
   registration (fixed in 4671a9e).
5. **Stooq's anonymous CSV endpoint is behind an anti-bot challenge now.** The adapter
   reports it plainly instead of 500ing the refresh (fixed in c56556b).
6. **Holdings showed raw document IDs** instead of security names (fixed in b22a857).

Screenshots and demo videos from this exact site are in
[screenshots/](screenshots/) and [videos/](videos/).

## Not executed anywhere

- Any real broker account, market-data key, or production portfolio data.
  Zerodha/Alpaca/IBKR-Flex parsing is fixture-tested; the sandbox OAuth flows are
  exercised against the UI only.
- A real Frappe Cloud Marketplace subscription. The plan reader is verified against a
  stubbed transport; the endpoint shape (`document_name`, `enabled`, `plan`, `site`)
  comes from reading `press/api/developer/marketplace.py`, and press does not publish
  it as a stable contract. Confirm it against a live subscription before the first
  paid customer.

## Known limits (by design in 0.1.0)

- Zerodha sync covers holdings/quotes/today's trades; history and dividends import via
  CSV (the Kite API does not expose them).
- Alpaca split activities arrive flagged `needs_review` when the ratio cannot be derived.
- Accounting posts journal entries for trades/income/fees only; splits, transfers and
  deposits do not post GL lines in v1.
- The license gate is a commercial control, not DRM (the app is MIT). Keys are signed
  offline with the publisher keypair (`scripts/make_license.py`) and verified locally;
  no data leaves the site.

Run everything from the repo root:

```sh
python -m pytest -q
ruff check .
node tests/js/investing.test.js
python scripts/preflight.py
python -m build && python scripts/check_dist.py
```
