# Frappe Investing

**Investment management for ERPNext — portfolios, broker sync, corporate actions, performance, and real accounting entries.**

Track stocks, ETFs and bonds with an auditable, event-sourced ledger inside ERPNext. Connect brokers automatically, or import CSV. Every position is derived from immutable investment events, every profit and loss can be traced back to its tax lots, and every accounting entry follows your own accounting policy.

**Standard tier is free and open source (MIT).** Pro adds crypto holdings — enforced in-app, not just on the price page.

> Status: implementation release candidate. Not yet on the Frappe Marketplace; not yet live-validated against a production broker account. See `docs/verification.md`.

## What you can do

- **Portfolios and accounts** — group broker accounts into portfolios per company, in any base currency.
- **Automatic broker sync** — Zerodha (Kite Connect), Alpaca, Interactive Brokers (Flex Web Service), plus a generic CSV importer.
- **Every event accounted for** — buys, sells, dividends, bond coupons, fees, deposits, withdrawals, transfers, stock splits, reverse splits, stock dividends, spin-offs, cash-in-lieu, redemptions.
- **Tax lots your way** — FIFO, LIFO, average cost, or specific identification; realized P&L per allocation.
- **Corporate actions done right** — a 4-for-1 split keeps your total cost basis exactly; spin-offs allocate basis explicitly; fractional shares sell through cash-in-lieu.
- **Performance that respects cash flows** — daily snapshots, time-weighted return (TWR), money-weighted XIRR, income totals, currency translation with explicit FX rates.
- **ERPNext accounting** — an Investment Accounting Policy maps each event type to your chart of accounts; journal entries post as drafts or submitted per policy, idempotently, and cancel with their events.
- **Fixed income, properly** — coupon schedules, accrued interest (30/360, ACT/ACT), clean/dirty prices, yield-to-maturity, redemption at maturity.

## Tiers

| | Standard (free) | Pro |
|---|---|---|
| Stocks, ETFs, bonds, funds | ✓ | ✓ |
| All broker connectors + CSV | ✓ | ✓ |
| Corporate actions, lots, performance, accounting | ✓ | ✓ |
| **Crypto asset class + crypto prices** | — | ✓ |

Pro is activated by an offline-signed license key (Investment License). No feature data leaves your site; verification is local. Open source means this is a commercial control, not DRM — see `docs/tiers.md` for the honest version.

## Data sources (zero mandatory cost)

- **Brokers are account truth** — trades, holdings, dividends and withholding come from your broker, with your credentials, on your site.
- **Prices:** manual entry, Stooq EOD (free, no key), broker quotes, or your own Alpha Vantage key. Crypto prices via CoinGecko (Pro).
- **Zerodha honesty:** the Kite API provides holdings, quotes and today's trades — not historical trades or dividends. Those import from Zerodha Console CSV exports. This is documented, not hidden.

## Getting started

1. Install the app on Frappe/ERPNext v15 or v16 (MariaDB).
2. Assign the **Investment User** or **Investment Manager** role.
3. Open **Investing** from the workspace, create a portfolio and an investment account.
4. Add securities manually, or connect a broker in **Broker Setup**.
5. Configure the **Investment Accounting Policy** for your company to post events to the ledger.

## Documentation

- [Architecture: events, lots, performance, accounting](docs/architecture.md)
- [Broker connectors and their honest limits](docs/connectors.md)
- [Market data and FX](docs/market-data.md)
- [Accounting policies](docs/accounting.md)
- [Tiers and licensing](docs/tiers.md)
- [Operations and upgrades](docs/operations.md)
- [Verification record](docs/verification.md)

## Not affiliated

Not affiliated with Zerodha, Alpaca, Interactive Brokers, Frappe or any market-data provider. Broker and exchange names belong to their owners. Nothing here is investment advice.

## License

MIT — see LICENSE.
