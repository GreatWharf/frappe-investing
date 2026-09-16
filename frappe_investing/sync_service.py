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
        created = {"events": 0, "positions": 0, "prices": 0}
        connector = connector_for(connection)
        capabilities = connector.capabilities()
        if capabilities.get("positions"):
            positions = connector.positions()
            created["positions"] = len(positions)
            created["prices"] = _store_position_prices(positions, connection)
        for event in _connector_events(connector, connection, capabilities):
            _name, was_created = record_event(_event_to_doc(connection, event))
            created["events"] += int(was_created)
        log.events_created = created["events"]
        log.positions_seen = created["positions"]
        log.prices_seen = created["prices"]
        log.status = "Success"
    except BrokerError as exc:
        status = "Token Expired" if exc.code in {"token_expired", "unauthorized"} else "Error"
        frappe.db.set_value("Broker Connection", name, {"status": status, "last_error": str(exc)})
        if log:
            log.error = str(exc)
            log.status = "Failed"
        raise
    finally:
        if log and log.name:
            frappe.flags.investing_internal = True
            log.finished_at = now_datetime()
            log.save(ignore_permissions=True)
            frappe.flags.investing_internal = False
        frappe.db.commit()
        try:
            lock.release()
        except Exception:
            pass


def _connector_events(connector, connection, capabilities):
    """Dispatch to each connector's real event source. Unknown shapes are reported, not dropped."""
    if connection.broker == "Zerodha":
        return connector.todays_trades()
    if connection.broker == "Alpaca":
        result = connector.activities(after=connection.sync_cursor or None)
        for row in result.get("skipped", []):
            frappe.log_error(title=f"Alpaca activity skipped: {connection.name}", message=f"{row}")
        return result["events"]
    if connection.broker == "Interactive Brokers":
        result = connector.fetch(connection.flex_query_id)
        for row in result.get("errors", []):
            frappe.log_error(title=f"IBKR Flex section not imported: {connection.name}", message=f"{row}")
        return result["events"]
    return []


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
    name = (
        frappe.get_doc(
            {
                "doctype": "Security",
                "security_name": security_key,
                "ticker": security_key,
                "asset_class": "Stock",
                "currency": frappe.get_cached_value("Company", company, "default_currency"),
                "status": "Active",
            }
        )
        .insert(ignore_permissions=True)
        .name
    )
    return name


def refresh_prices(provider=None, securities=None):
    """Dispatch to the configured market-data provider. Idempotent per security/day."""
    conf = settings()
    provider = provider or conf.price_provider
    if provider == "Manual":
        return {"updated": 0, "note": "Manual pricing selected."}
    if provider == "CoinGecko (Pro)":
        from .license_service import require_feature

        require_feature("crypto")
        adapter = coingecko.CoinGeckoProvider(gate=lambda feature: True)
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
