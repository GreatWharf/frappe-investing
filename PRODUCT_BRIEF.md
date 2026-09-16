# Frappe Investing — product brief

**Status:** implementation in progress · **Started:** 16 September 2026 · **Owner session:** the frappe-investing fork (frappe-intelligence is owned by another session — do not cross the boundary)

## Promise

**Investment management for ERPNext.** Portfolios of stocks, ETFs and bonds with broker sync, corporate actions, tax lots, real performance math, and accounting entries that land in the ERPNext general ledger — auditable from journal entry back to broker event. Crypto holdings are the paid Pro tier.

## What makes it an ERP module instead of a tracker

1. **Event-sourced ledger.** Positions are never edited; they are derived from immutable submitted Investment Events (buy, sell, dividend, coupon, fee, split, spin-off, transfer…). Corrections are reversal events, so the audit trail is complete.
2. **The broker is account truth.** Connectors normalize broker data into events. Market-data providers supply prices and *announced* corporate actions; the broker's confirmed cash and quantities win.
3. **Accounting policy is configuration.** An Investment Accounting Policy maps each event type to debit/credit ERPNext accounts, so IFRS/GAAP/management-reporting differences are handled without code changes. Daily price moves do **not** spray journal entries — only economic events do.
4. **Real performance.** Daily snapshots, chain-linked TWR, XIRR, realized vs unrealized P&L, income return, multi-currency with FX translation.
5. **Free to run.** No mandatory data bill: Stooq/manual/broker quotes for EOD prices; brokers and CSV for events; optional Alpha Vantage/CoinGecko keys are the user's own.

## Tiers

Frappe Cloud plan feature lists do not enforce functionality, so tiers are enforced in-app with an offline, Ed25519-signed license key (`Investment License` single DocType). **Standard (free, open source):** stocks, ETFs, bonds, all brokers, imports, performance, accounting. **Pro (paid):** crypto asset class + crypto market data. Open source means a determined user can patch the check; the license is a commercial control, not DRM — documented in `docs/tiers.md`.

## Connectors (v1)

| Connector | Auth | Syncs | Honest limits |
|---|---|---|---|
| Zerodha Kite Connect | API key/secret, daily access-token login | Holdings, positions, today's orders/trades, quotes | Kite API has **no historical trades and no dividend feed** — history and dividends come from Console CSV import |
| Alpaca | Key/secret headers | Positions, account activities (fills, dividends, splits) | US equities; sandbox default off |
| Interactive Brokers Flex | Flex token + saved query | Trades, cash (dividends/WHT/fees), corporate actions | Query must be created in IBKR first; two-step fetch |
| Generic CSV | File upload | Any event type | Template-driven, dry-run before commit |

## Architecture

```
Broker APIs / CSV / Manual entry
        ↓  normalized
Investment Event (immutable, idempotent by source_ref)
        ↓
Lot engine (FIFO / Average / Specific ID) → Tax Lots, realized P&L
Income engine (gross / WHT / net, DRIP)  → income records
Corporate actions (split, reverse, stock dividend, spin-off, cash-in-lieu)
        ↓
Positions (derived) + Security Prices + FX → Valuation, Snapshots, TWR/XIRR
        ↓
Investment Accounting Policy → ERPNext Journal Entries (idempotent, reviewable)
```

## Scope fences (v1)

- No options/futures/margin/short selling. No real-time prices. No automatic tax filing.
- Bonds: coupon schedule, accrued interest (30/360 + actual/actual), clean/dirty price, redemption, YTM solver. No amortized-cost GL automation in v1.
- Corporate actions implemented: split, reverse split, stock dividend, spin-off (with basis allocation %), cash-in-lieu, redemption, symbol note. Rights issues/mergers/convertibles are v1.5 — the event framework accepts them, the lot transforms are explicit, not implied.
