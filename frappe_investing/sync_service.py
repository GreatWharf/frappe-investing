"""Broker sync, market-data refresh and imports. Holds per-connection locks; never half-commits a batch."""

import frappe
from frappe.utils import now_datetime

from .connectors import alpaca, ibkr_flex, zerodha
from .connectors.base import BrokerError
from .marketdata import alphavantage, coingecko, stooq
from .marketdata.base import MarketDataError
from .services import record_event, settings


class SyncError(ValueError):
    pass


def connection_lock(name):
    return frappe.cache.lock(f"investing-sync:{frappe.local.site}:{name}", timeout=900, blocking_timeout=0)


def connector_for(connection):
    if connection.broker == "Zerodha":
        token = connection.get_password("access_token", raise_exception=False)
        return zerodha.ZerodhaConnector(
            api_key=connection.api_key,
            api_secret=connection.get_password("api_secret", raise_exception=False) or "",
            access_token=token,
        )
    if connection.broker == "Alpaca":
        return alpaca.AlpacaConnector(
            api_key=connection.api_key,
            api_secret=connection.get_password("api_secret", raise_exception=False) or "",
            sandbox=bool(connection.sandbox),
        )
    if connection.broker == "Interactive Brokers":
        return ibkr_flex.IBKRFlexConnector(
            token=connection.get_password("flex_token", raise_exception=False) or "",
            query_id=connection.flex_query_id,
        )
    raise SyncError(f"{connection.broker} has no automatic sync; use CSV Import.")


def sync_connection(name):
    """Sync one connection under a lock; results land in a Sync Log either way."""
    connection = frappe.get_doc("Broker Connection", name)
    connection.check_permission("write")
    if not connection.enabled:
        raise SyncError("Enable the connection before syncing.")
    lock = connection_lock(name)
    if not lock.acquire(blocking=False):
        raise SyncError("A sync is already running for this connection.")
    log = None
    log_inserted = False
    created = {"events": 0, "positions": 0, "prices": 0}
    ok = False
    try:
        log = frappe.get_doc(
            {
                "doctype": "Broker Sync Log",
                "connection": name,
                "started_at": now_datetime(),
                "status": "Failed",
            }
        )
        frappe.flags.investing_internal = True
        log.insert(ignore_permissions=True)
        log_inserted = True
        created = {"events": 0, "positions": 0, "prices": 0}
        connector = connector_for(connection)
        capabilities = connector.capabilities()
        if capabilities.get("positions"):
            positions = connector.positions()
            created["positions"] = len(positions)
            created["prices"] = _store_position_prices(positions, connection)
        events, truncated, cursor = _connector_events(connector, connection, capabilities)
        for event in events:
            _name, was_created = record_event(_event_to_doc(connection, event))
            created["events"] += int(was_created)
        log.events_created = created["events"]
        log.positions_seen = created["positions"]
        log.prices_seen = created["prices"]
        if truncated:
            log.status = "Partial"
            log.error = (
                f"Source truncated the event window; {created['events']} events recorded, "
                "re-run syncs the remainder from the saved cursor."
            )
        else:
            log.status = "Success"
        if cursor and not truncated:
            # Advance the stream only on a complete run: a truncated run
            # replays the same window next time, dedupe discarding repeats.
            frappe.db.set_value("Broker Connection", name, "sync_cursor", cursor)
        frappe.db.set_value(
            "Broker Connection",
            name,
            {"status": "Connected", "last_sync": now_datetime(), "last_error": ""},
        )
        frappe.db.commit()
        ok = True
    except BrokerError as exc:
        status = "Token Expired" if exc.code in {"token_expired", "unauthorized"} else "Error"
        if log:
            log.error = str(exc)
            log.status = "Failed"
        frappe.db.rollback()
        # Re-apply the status AFTER the rollback (which discards the in-batch
        # set_value) so the connection never stays stuck on Connected.
        frappe.db.set_value("Broker Connection", name, {"status": status, "last_error": str(exc)})
        frappe.db.commit()
        raise
    except Exception as exc:
        # Anything else (SyncError, ValidationError, a connector bug) must
        # still leave a cause on the log and the connection — never a blank
        # Failed log with a connection stuck on Connected.
        if log:
            log.error = str(exc)
            log.status = "Failed"
        frappe.db.rollback()
        frappe.db.set_value("Broker Connection", name, {"status": "Error", "last_error": str(exc)})
        frappe.db.commit()
        raise
    finally:
        # On failure the rollback above removed the in-flight batch AND the
        # log row, so re-insert a fresh Failed log instead of saving the dead
        # row (its UPDATE would hit zero rows and silently change nothing).
        try:
            if log and log.name:
                frappe.flags.investing_internal = True
                log.finished_at = now_datetime()
                if not ok and log_inserted:
                    fresh = frappe.get_doc(
                        {
                            "doctype": "Broker Sync Log",
                            "connection": name,
                            "started_at": log.started_at,
                            "status": "Failed",
                            "error": log.error,
                            "events_created": created["events"],
                            "finished_at": log.finished_at,
                        }
                    )
                    fresh.insert(ignore_permissions=True)
                else:
                    log.save(ignore_permissions=True)
                frappe.flags.investing_internal = False
                frappe.db.commit()
        finally:
            try:
                lock.release()
            except Exception:
                pass


def _connector_events(connector, connection, capabilities):
    """Dispatch to each connector's real event source. Unknown shapes are reported, not dropped.

    Returns (events, truncated, cursor): ``truncated`` when the source capped
    the window (callers mark the run Partial and keep the old cursor);
    ``cursor`` advances the stream past this window on complete runs.
    """
    if connection.broker == "Zerodha":
        result = connector.todays_trades()
        return result["events"], result.get("truncated", False), None
    if connection.broker == "Alpaca":
        result = connector.activities(after=connection.sync_cursor or None)
        for row in result.get("skipped", []):
            frappe.log_error(title=f"Alpaca activity skipped: {connection.name}", message=f"{row}")
        cursor = None
        dates = [e.get("date") for e in result.get("events", []) if e.get("date")]
        if dates:
            cursor = max(dates)
        return result["events"], result.get("truncated", False), cursor
    if connection.broker == "Interactive Brokers":
        result = connector.fetch(connection.flex_query_id)
        for row in result.get("errors", []):
            frappe.log_error(title=f"IBKR Flex section not imported: {connection.name}", message=f"{row}")
        return result["events"], result.get("truncated", False), None
    return [], False, None


def _store_position_prices(positions, connection):
    """Broker quotes come from holdings/positions last prices where available."""
    count = 0
    day = frappe.utils.today()
    for position in positions:
        if not position.get("market_price"):
            continue
        security = _resolve_security(position["security_key"], connection.company)
        if frappe.db.exists("Security Price", {"security": security, "date": day}):
            continue
        frappe.flags.investing_internal = True
        frappe.get_doc(
            {
                "doctype": "Security Price",
                "security": security,
                "date": day,
                "close": position["market_price"],
                "currency": position.get("currency") or _resolve_currency(connection),
                "source": "Broker",
            }
        ).insert(ignore_permissions=True)
        frappe.flags.investing_internal = False
        count += 1
    return count


def _resolve_currency(connection):
    return frappe.get_cached_value("Company", connection.company, "default_currency")


def _event_to_doc(connection, event):
    """Map a normalized connector event to Investment Event fields."""
    account = frappe.db.get_value(
        "Investment Account", {"broker_connection": connection.name, "enabled": 1}, "name"
    )
    if not account:
        raise SyncError(f"No enabled Investment Account is linked to {connection.name}.")
    security = _resolve_security(event.get("security_key"), connection.company)
    return {
        "event_type": event["type"],
        "posting_date": event["date"],
        "account": account,
        "security": security,
        "qty": event.get("qty"),
        "price": event.get("price"),
        "amount": event.get("amount"),
        "gross": event.get("gross"),
        "fees": event.get("fees"),
        "taxes": event.get("taxes"),
        "currency": event.get("currency") or "INR",
        "source": connection.broker,
        "source_ref": event.get("source_ref"),
        "connection": connection.name,
        "split_ratio": event.get("split_ratio"),
        "meta_json": frappe.as_json(event.get("meta") or {}),
        "notes": event.get("notes", ""),
    }


def _resolve_security(security_key, company):
    """Find or create a Security from a connector key like NSE:RELIANCE or ISIN:... ."""
    if not security_key:
        return None
    existing = frappe.db.get_value("Security", {"ticker": security_key}, "name")
    if existing:
        return existing
    if security_key.startswith("ISIN:"):
        existing = frappe.db.get_value("Security", {"isin": security_key[5:]}, "name")
        if existing:
            return existing
    from .license_service import require_asset_class

    asset_class = "Crypto" if security_key.startswith("CRYPTO:") else "Stock"
    require_asset_class(asset_class)  # auto-created securities count toward the tier limit too
    name = (
        frappe.get_doc(
            {
                "doctype": "Security",
                "security_name": security_key,
                "ticker": security_key,
                "asset_class": asset_class,
                "currency": frappe.get_cached_value("Company", company, "default_currency"),
                "status": "Active",
            }
        )
        .insert(ignore_permissions=True)
        .name
    )
    return name


def crypto_gate(feature):
    """Real tier gate for the CoinGecko adapter: crypto needs a tier upgrade.

    Returns True when the current license already covers a second asset class
    beyond what is tracked (or is unlimited); False otherwise, so the adapter
    raises its pro-tier MarketDataError instead of fetching. Any feature flag
    other than "crypto" fails closed.
    """
    if feature != "crypto":
        return False
    from . import license_service

    state = license_service.current_state()
    if state.max_asset_classes is None:
        return True
    return len(license_service.used_asset_classes()) < state.max_asset_classes


def crypto_holdings_requested():
    """True when crypto prices are actually needed: an Active Crypto Security
    carries a provider id, or an open lot/income-relevant holding exists."""
    rows = frappe.get_all(
        "Security",
        filters={"status": "Active", "asset_class": "Crypto", "coingecko_id": ["!=", ""]},
        fields=["name"],
        limit_page_length=1,
    )
    return bool(rows)


def refresh_prices(provider=None, securities=None):
    """Dispatch to the configured market-data provider. Idempotent per security/day."""
    conf = settings()
    provider = provider or conf.price_provider
    if provider == "Manual":
        return {"updated": 0, "note": "Manual pricing selected."}
    if provider == "CoinGecko":
        from .license_service import require_asset_class

        # Only CoinGecko refreshes that actually touch crypto require the
        # class: a site tracking one non-crypto class with no crypto
        # securities or holdings refreshes freely.
        if crypto_holdings_requested():
            require_asset_class("Crypto")  # pricing crypto counts as tracking the class
        adapter = coingecko.CoinGeckoProvider(gate=crypto_gate)
        symbol_field = "coingecko_id"
    elif provider == "Stooq":
        adapter = stooq.StooqProvider()
        symbol_field = "stooq_symbol"
    elif provider == "Alpha Vantage":
        adapter = alphavantage.AlphaVantageProvider(api_key=frappe.conf.get("investing_alpha_key", ""))
        symbol_field = "alphavantage_symbol"
    elif provider == "Broker":
        return {"updated": 0, "note": "Broker prices are stored during broker sync."}
    else:
        raise SyncError(f"Unknown price provider: {provider}")
    updated = 0
    rows = frappe.get_all(
        "Security",
        filters={"status": "Active"},
        fields=["name", "currency", symbol_field],
        limit_page_length=500,
    )
    for row in rows:
        if not row.get(symbol_field):
            continue
        try:
            quotes = adapter.daily_prices([row[symbol_field]], frappe.utils.today())
        except MarketDataError as exc:
            frappe.log_error(title=f"Price refresh failed for {row.name}", message=str(exc))
            continue
        for quote in quotes:
            if frappe.db.exists("Security Price", {"security": row.name, "date": quote["day"]}):
                continue
            frappe.flags.investing_internal = True
            frappe.get_doc(
                {
                    "doctype": "Security Price",
                    "security": row.name,
                    "date": quote["day"],
                    "close": quote["close"],
                    "currency": quote.get("currency") or row.currency,
                    "source": "CoinGecko" if "CoinGecko" in provider else provider,
                }
            ).insert(ignore_permissions=True)
            frappe.flags.investing_internal = False
            updated += 1
    return {"updated": updated}


def refresh_benchmark_prices(codes=None, day=None):
    """Fetch benchmark index prices from Stooq (free, no key) and store them.

    Idempotent per benchmark/day. Benchmarks are catalog Securities; their
    prices attach to the BENCH:<code> ticker. Returns {code: close or None}.
    """
    from .core import benchmarks
    from .services import _benchmark_security

    day = day or frappe.utils.today()
    wanted = [benchmarks.get(c) for c in (codes or benchmarks.codes())]
    wanted = [b for b in wanted if b]
    if not wanted:
        return {}
    adapter = stooq.StooqProvider()
    out = {}
    for bench in wanted:
        security = _benchmark_security(bench)
        if frappe.db.exists("Security Price", {"security": security, "date": day}):
            out[bench["code"]] = frappe.db.get_value(
                "Security Price", {"security": security, "date": day}, "close"
            )
            continue
        try:
            quotes = adapter.daily_prices([bench["stooq"]], day)
        except MarketDataError as exc:
            frappe.log_error(title=f"Benchmark price failed for {bench['code']}", message=str(exc))
            out[bench["code"]] = None
            continue
        if not quotes:
            out[bench["code"]] = None
            continue
        frappe.flags.investing_internal = True
        try:
            frappe.get_doc(
                {
                    "doctype": "Security Price",
                    "security": security,
                    "date": day,
                    "close": quotes[0]["close"],
                    "currency": bench["currency"],
                    "source": "Stooq",
                }
            ).insert(ignore_permissions=True)
        finally:
            frappe.flags.investing_internal = False
        out[bench["code"]] = quotes[0]["close"]
    return out


def scheduled_broker_sync():
    if not settings().broker_sync_enabled:
        return
    for name in frappe.get_all("Broker Connection", filters={"enabled": 1}, pluck="name"):
        frappe.enqueue(
            "frappe_investing.sync_service.sync_connection",
            name=name,
            queue="long",
            job_id=f"investing-sync-{name}",
            deduplicate=True,
        )
