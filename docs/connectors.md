# Broker connectors

Connectors translate broker data into normalized events. They never implement portfolio logic — the Investment Ledger does.

## Zerodha (Kite Connect)

**Auth:** API key + secret from the Kite developer console → login URL → redirect `request_token` → exchanged server-side for a daily `access_token` (checksum = sha256(api_key + request_token + api_secret)). Tokens expire each trading day; reconnect from the connection card.

**Syncs:** holdings, positions, today's orders/trades, quotes, instrument master per exchange.

**Not available from the API:** historical trades and dividends. Import those from Zerodha **Console → Reports** CSV using the CSV importer (tradebook for history; tax P&L / dividends exports for income). This limitation is Zerodha's, not ours, and it is surfaced in the UI.

## Alpaca

Key/secret headers; positions and account activities (fills, dividends, splits) with pagination. Sandbox (`paper-api`) is available via the connection's Sandbox checkbox. Split activities that cannot compute a ratio are flagged `needs_review` instead of guessed.

## Interactive Brokers (Flex Web Service)

Create a Flex query in IBKR (Activity Flex, XML), generate a Flex token, and put both on the connection. Sync runs SendRequest → reference code → GetStatement, parsing trades, cash transactions (dividends + withholding tax matched by date/symbol) and corporate actions. Unrecognized sections are reported, never silently dropped.

## Generic CSV

Template columns (download it from the investing page — headers plus one example row): `date,type,security_key,qty,price,amount,gross,fees,taxes,withholding,currency,accrued_interest,split_ratio,basis_allocation,child_security,child_ratio,lot_ids,target_currency,target_amount,notes,source_ref`. Rows validate against the same Event rules as broker data; missing `source_ref` gets a deterministic row hash; imports are all-or-nothing after a dry run.

**Withholding tax:** `taxes` and `withholding` are the same field — fill exactly one (filling both is a row error). On Dividend/Coupon/Interest the amount flows to `Event.taxes`, and accounting books it to the policy's withholding-tax receivable (`tax_debit_account`) when configured, otherwise through the cash account. See accounting.md.

**Capital-gains cost method:** the import dialog shows the effective method for the picked account — the portfolio's Cost Method override, else the Investment Settings default (FIFO / LIFO / AVERAGE / SPECIFIC). The selector documents the method; the method itself lives on the Portfolio, because `services.cost_method_for` reads it at posting time for every event, imported or manual.

**FX Conversion** needs `amount` + `target_currency` + `target_amount`; bond **Buy/Sell** accepts `accrued_interest`; **SPECIFIC**-method sells accept `lot_ids` (semicolon-separated Tax Lot engine IDs).

## Security identity

Connector keys (`NSE:RELIANCE`, `US:AAPL`, `ISIN:…`, `CRYPTO:BTC`, `SMART:…`) resolve to Security records by ticker/ISIN; unknown ones are created as draft Stocks so imports never fail mid-batch — review and classify them afterwards. (Statement CSV import differs deliberately: unknown tickers are row errors, because a statement file should never mint Securities silently — create them first, then re-run the preview.)
