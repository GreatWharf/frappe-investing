# Frappe Investing — implementation plan

**Stack:** Python ≥3.10,<3.15 · Frappe v15/v16 · MariaDB · dependency-free Desk JS · requests + cryptography as runtime deps.

## Boundaries and contracts

- **Pure core** (`frappe_investing/core/`, `connectors/`, `marketdata/`, `licensing.py`): no `import frappe` except at the Frappe-boundary modules (`services.py`, `api.py`, `install.py`, controllers). All money is `Decimal`, context precision 28. No floats cross a function boundary.
- **Events immutable after submit.** `source_ref` unique per (connection, account) for idempotent broker/CSV sync. Reversal events reference the original and negate it; never delete history.
- **Lots:** FIFO default; Average pools basis per (account, security); Specific ID requires lot selection on sell. Split ratio is new/old Decimal. Spin-off requires explicit `basis_allocation_percent`.
- **Accounting:** Investment Event submit → accounting service builds JE from policy mappings → draft or submitted per policy → event.journal_entry link. Cancel event → cancel JE first. One JE per event enforced by unique source.
- **Tier gate:** `licensing.require_feature("crypto")` called at Security save (asset_class=Crypto), CoinGecko adapter, and crypto dashboard sections. Free tier must be fully useful without it.
- **Permissions:** roles `Investment User` (read/enter) and `Investment Manager` (+ connections, policies, license); System Manager admin. Portfolios scoped to Company + owner. Server-side checks on every whitelisted method.
- **Idempotency:** every external write (JE, event insert) carries deterministic dedupe key; broker sync holds a per-connection lock and checkpoints cursor.

## File map

```
frappe_investing/
  core/          events.py lots.py income.py bonds.py valuation.py performance.py fx.py accounting.py money.py
  connectors/    base.py zerodha.py alpaca.py ibkr_flex.py csv_import.py
  marketdata/    base.py stooq.py alphavantage.py coingecko.py manual.py
  licensing.py   (Ed25519 offline license, tiers)
  services.py    event/lot/valuation/accounting orchestration (frappe boundary)
  api.py         whitelisted dashboard/setup methods
  install.py     roles, workspace, indexes, settings defaults
  hooks.py
  frappe_investing/doctype/... (generated schema)
  public/js+css  investing dashboard, broker setup
tests/           pytest offline suite + fixtures
scripts/         generate_schema.py, check_dist.py, make_license.py (publisher tool)
```

## Milestones

1. Core engine + tests (lots, splits, dividends, accrued interest, TWR/XIRR, FX, policy→JE lines).
2. Licensing + market data + connectors (fixture tests, no live calls).
3. Frappe schema/controllers/services/hooks/install + permission tests.
4. Dashboard + broker setup UI + Node tests.
5. Docs (README marketing-honest, architecture, connectors, accounting, tiers, operations), packaging, CI workflows, full verification, review pass.
