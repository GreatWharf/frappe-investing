# Architecture

## The Investment Ledger

Everything derives from **Investment Events**. A position is never edited; it is recomputed from events.

```
Broker sync / CSV / manual entry
        ↓  normalized + validated
Investment Event (submitted, immutable)
        ↓
Lot engine → Tax Lots + Lot Allocations (realized P&L)
Income events → gross / withholding / net
Corporate actions → lot transforms (basis preserved)
        ↓
Positions × Security Prices × FX → valuation, snapshots, TWR/XIRR
        ↓
Investment Accounting Policy → ERPNext Journal Entries
```

### Events

`frappe_investing/core/events.py` defines the normalized taxonomy: Buy, Sell, Dividend, Coupon, Interest, Fee, Deposit, Withdrawal, Transfer In/Out, Split, Reverse Split, Stock Dividend, Spin-off, Cash-in-lieu, Redemption, FX Conversion. Quantities are positive; the type carries direction. `cash_flow()` defines the sign convention. All numbers are `Decimal`; floats are rejected at the boundary.

Idempotency: every event carries `(account, source, source_ref)` hashed into `dedupe_key` (unique). Replaying a broker sync or CSV import is safe.

### Order and correction

Events apply in posting-date order per account/security; the controller rejects an event dated before an existing later event. To correct: cancel events from newest to oldest (cancelling restores lots from allocations and cancels the linked Journal Entry first). There is no silent edit path.

### Lots

`core/lots.py` implements FIFO, LIFO, AVERAGE (basis pooling per account/security) and SPECIFIC (sell names the lots). Corporate actions preserve total basis: a 4:1 split turns 25 shares @ £400 into 100 @ £100 — basis stays £10,000. Spin-offs move an explicit `basis_allocation` fraction of parent basis into child lots. Stock dividends dilute basis by default (zero-cost lots are available for jurisdictions that want them).

### Performance

- **TWR** chains daily returns and excludes external flows (deposits are not returns).
- **XIRR** solves actual/365 discounted cash flows by bisection; deposits are negative flows, withdrawals positive, terminal value is the current portfolio value.
- Valuation flags stale/missing prices instead of zeroing them.

### Accounting

`core/accounting.py` maps an event plus its lot allocations to balanced Journal Entry lines under an **Investment Accounting Policy** per company (event type → debit/credit/tax/fee/P&L accounts). Fees capitalize into basis on buys and reduce proceeds on sells; withholding tax posts to a receivable when configured. Market-value movements never post — only economic events do. Policy mode Off/Draft/Submit controls JE posting; mapping failures mark the event `Failed` and log, never guess.

## Trust boundaries

- Connectors and market-data adapters are pure Python: no `frappe` imports, injectable HTTP sessions, sanitized errors, size/time caps, no redirects on token endpoints.
- Broker credentials are Frappe Password fields. Zerodha access tokens expire daily (Kite design) — the UI says so.
- The tier gate is evaluated offline (Ed25519) and checked server-side at Security save (asset_class=Crypto), event validation, and the CoinGecko adapter.
