# Market data and FX

The app is free to run: no mandatory data subscription.

## Prices

| Source | Key needed | Notes |
|---|---|---|
| Manual | — | Enter Security Price records yourself; always available. |
| Broker | your broker credentials | Quotes stored during broker sync where the connector supports them. |
| Stooq | — | Free end-of-day CSV. The Security's `stooq_symbol` (e.g. `aapl.us`) drives lookup; Stooq CSV does not state currency, so the Security's own currency applies. |
| Alpha Vantage | your free key | Adapter is opt-in with each site's own key. Alpha Vantage's default terms are personal/non-commercial; their open-source allowance requires written confirmation — do not assume it. |
| CoinGecko | — | Crypto prices. Crypto counts toward the license's asset-class limit. Public API is rate-limited. |

Prices are EOD-only by design. This is an accounting system, not a trading terminal; realtime exchange licensing is deliberately out of scope.

## FX

FX Rate records are explicit (from/to/date/rate). Valuation converts with the latest rate on or before the day; a missing rate fails loudly and the security is flagged stale — values are never silently zeroed or guessed.

## Corporate-action sources, in precedence order

1. Broker-confirmed (cash/quantities actually moved) — accounting truth.
2. Manual entry by a manager.
3. Market-data/provider announcement — context only; never overrides broker cash.
