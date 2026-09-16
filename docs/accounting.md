# Accounting

Investments are tracked continuously in the ledger; the GL receives economic events only.

## The policy

**Investment Accounting Policy** (one per company) maps each event type to ERPNext accounts:

| Event | Typical debit | Typical credit | Optional lines |
|---|---|---|---|
| Buy | Equity Investments | Broker Cash | fees capitalize into basis; accrued interest line for bonds |
| Sell | Broker Cash | Equity Investments (at lot cost) | realized gain/loss to P&L account |
| Dividend / Coupon / Interest | Broker Cash (net) | Dividend/Interest Income (gross) | withholding tax receivable; fee expense |
| Fee | Brokerage Fees | Broker Cash | |

Deposits, withdrawals, transfers, splits and other non-cash corporate actions post no journal lines in v1 — moving money between your own accounts is a Cash/Bank transaction decision, and splits move no money.

## Guarantees

- Lines must balance (debit = credit in company currency) or the event is marked `Failed` with a logged error — nothing half-posts.
- One Journal Entry per event; cancelling an event cancels its JE first.
- Mode `Off` records accounting status `Skipped`; `Draft` creates reviewable drafts; `Submit` posts immediately.
- Multi-currency: lines carry company-currency amounts converted at the FX Rate on or before the event date. A missing rate fails closed with an explicit error.

## Corrections

Cancel the newest event for the account/security; its JE cancels and its lot allocations restore. Later events must be cancelled first — this is deliberate, and it is what keeps an audit trail intact.
