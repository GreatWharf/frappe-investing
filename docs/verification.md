# Verification record — 2026-09-16

## Executed locally

| Check | Result |
|---|---|
| Offline pytest suite (core engine, lots, corporate actions, income, bonds, FX, valuation, TWR/XIRR, accounting mapper, licensing, services orchestration, connectors, market data, schema) | **128 passed** |
| Ruff lint | Passed |
| Python 3.10 grammar check (all app/test/script files) | Passed |
| Connector/market-data HTTP error sanitization (secrets never in error strings) | Tested with mocked transports |
| Zerodha/Alpaca/IBKR-Flex/CSV parsing | Fixture tests only — **no live broker calls** |
| Distribution wheel + source archive contents | Checked by `scripts/check_dist.py` |

## Not executed here

- A live Frappe/ERPNext site install, migration or browser session.
- Any real broker account, market-data key, or production portfolio data.
- The bench integration workflow (`frappe_investing.tests.test_integration`) — it exists for CI; run it before treating a release as production-proven.
- License key generation for production (the embedded public key is a development placeholder until the publisher generates the real keypair with `scripts/make_license.py`).

## Known limits (by design in 0.1.0)

- Zerodha sync covers holdings/quotes/today's trades; history and dividends import via CSV (Kite API does not expose them).
- Alpaca split activities arrive flagged `needs_review` when the ratio cannot be derived.
- Accounting posts journal entries for trades/income/fees only; splits, transfers and deposits do not post GL lines in v1.
- The license gate is a commercial control, not DRM (the app is MIT).

Run everything from the repo root:

```sh
python -m pytest -q
ruff check .
node --test tests/js/investing.test.js
python -m build && python scripts/check_dist.py
```
