"""Zerodha Kite Connect v3 connector.

Honest limits of the Kite API (all reflected in ``capabilities()``):

* **no historical trades** — Kite serves today's orders and trades only;
  older executions are not available in the API. History arrives via CSV
  import of Zerodha Console tradebook exports.
* **no dividend feed** — dividends and other income arrive via CSV import
  of Console P&L / dividend exports.
* The access token expires daily: call ``login_url()``, then
  ``exchange_token(request_token)`` after the user completes login.
* Per-trade statutory charges (STT, stamp duty, GST, exchange fees) are not
  part of the trades payload, so events carry ``fees="0"``/``taxes="0"`` and
  a ``charges_note`` in meta; reconcile charges from Console exports.

security_key convention: ``NSE:RELIANCE`` — ``{exchange}:{tradingsymbol}``.
Holdings and trades are INR-denominated.
"""

import csv
import hashlib
import io
from datetime import date
from urllib.parse import quote

from .base import BrokerError, ConnectorBase, http_json, http_request, normalized_event

API_BASE = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
KITE_VERSION = "3"

# Cap on per-order trade fetches per sync: a busy day cannot fan out
# unboundedly inside one worker job.
MAX_TRADE_ORDER_FETCHES = 200


class ZerodhaConnector(ConnectorBase):
    """Kite Connect v3: token login, holdings, today's orders/trades, instrument master."""

    name = "zerodha"
    supported = frozenset({"positions", "trades"})

    def __init__(self, api_key, api_secret, access_token=None, session=None):
        super().__init__(session=session)
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token

    def login_url(self):
        """URL the user opens to authenticate; Kite redirects back with a request_token."""
        return f"{LOGIN_URL}?api_key={quote(self.api_key, safe='')}&v={KITE_VERSION}"

    def exchange_token(self, request_token):
        """Trade the one-time request_token for today's access_token (POST /session/token)."""
        checksum = hashlib.sha256(
            (self.api_key + request_token + self.api_secret).encode("utf-8")
        ).hexdigest()
        payload = http_json(
            self.session,
            "POST",
            f"{API_BASE}/session/token",
            data={"api_key": self.api_key, "request_token": request_token, "checksum": checksum},
            headers={"X-Kite-Version": KITE_VERSION},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("status") != "success" or not isinstance(data, dict) or not data.get("access_token"):
            raise BrokerError("Kite token exchange failed", code="auth")
        self.access_token = data["access_token"]
        return self.access_token

    def _headers(self):
        if not self.access_token:
            raise BrokerError("Kite access_token is not set; run exchange_token() first", code="auth")
        return {"X-Kite-Version": KITE_VERSION, "Authorization": f"token {self.api_key}:{self.access_token}"}

    def _get_data(self, path):
        payload = http_json(self.session, "GET", f"{API_BASE}{path}", headers=self._headers())
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise BrokerError(f"Kite call to {path} did not succeed", code="bad_response")
        return payload.get("data")

    def holdings(self):
        """GET /portfolio/holdings -> normalized position dicts (all INR)."""
        positions = []
        for row in self._get_data("/portfolio/holdings") or []:
            avg_cost = row.get("average_price")
            last_price = row.get("last_price")
            positions.append(
                {
                    "security_key": f"{row.get('exchange', '')}:{row.get('tradingsymbol', '')}",
                    "qty": str(row.get("quantity", "0")),
                    "avg_cost": str(avg_cost) if avg_cost is not None else None,
                    "currency": "INR",
                    "market_price": str(last_price) if last_price is not None else None,
                    "as_of": date.today().isoformat(),
                }
            )
        return positions

    def positions(self):
        """GET /portfolio/positions -> normalized position dicts (all INR).

        Kite wraps rows in {"net": [...], "day": [...]}; day rows are excluded
        (zero overnight quantity) and only net rows become positions.
        """
        data = self._get_data("/portfolio/positions") or {}
        rows = data.get("net", []) if isinstance(data, dict) else []
        positions = []
        for row in rows:
            avg_cost = row.get("average_price")
            last_price = row.get("last_price")
            positions.append(
                {
                    "security_key": f"{row.get('exchange', '')}:{row.get('tradingsymbol', '')}",
                    "qty": str(row.get("quantity", "0")),
                    "avg_cost": str(avg_cost) if avg_cost is not None else None,
                    "currency": "INR",
                    "market_price": str(last_price) if last_price is not None else None,
                    "as_of": date.today().isoformat(),
                }
            )
        return positions

    def todays_orders(self):
        """GET /orders -> today's orders in a normalized minimal shape (all statuses)."""
        orders = []
        for row in self._get_data("/orders") or []:
            orders.append(
                {
                    "order_id": str(row.get("order_id", "")),
                    "status": row.get("status", ""),
                    "side": row.get("transaction_type", ""),
                    "security_key": f"{row.get('exchange', '')}:{row.get('tradingsymbol', '')}",
                    "qty": str(row.get("quantity", "0")),
                    "average_price": str(row.get("average_price", "0")),
                    "order_timestamp": row.get("order_timestamp", ""),
                }
            )
        return orders

    def todays_trades(self):
        """Today's executions -> Buy/Sell event dicts (source_ref = trade_id).

        Fetches /orders once, then /orders/{order_id}/trades for COMPLETE
        orders only. OPEN/REJECTED orders have no executions and are skipped.
        Per-order fetches are capped at MAX_TRADE_ORDER_FETCHES; the tail
        is reported via ``truncated`` so callers never silently drop trades.
        """
        events = []
        fetched, truncated = 0, False
        for order in self.todays_orders():
            if order["status"] != "COMPLETE":
                continue
            if fetched >= MAX_TRADE_ORDER_FETCHES:
                truncated = True
                break
            fetched += 1
            for trade in self._get_data(f"/orders/{order['order_id']}/trades") or []:
                side = str(trade.get("transaction_type", "")).upper()
                if side not in {"BUY", "SELL"}:
                    continue
                events.append(
                    normalized_event(
                        "Buy" if side == "BUY" else "Sell",
                        security_key=f"{trade.get('exchange', '')}:{trade.get('tradingsymbol', '')}",
                        qty=str(trade.get("quantity", "0")),
                        price=str(trade.get("average_price", "0")),
                        currency="INR",
                        date=str(trade.get("fill_timestamp", ""))[:10],
                        source_ref=str(trade.get("trade_id", "")),
                        meta={
                            "order_id": str(trade.get("order_id", "")),
                            "charges_note": "statutory charges not included; reconcile via Console CSV",
                        },
                    )
                )
        return {"events": events, "truncated": truncated, "fetched_orders": fetched}

    def instruments(self, exchange):
        """GET /instruments/{exchange} -> parsed instrument master rows for security search."""
        body = http_request(
            self.session, "GET", f"{API_BASE}/instruments/{exchange}", headers=self._headers()
        )
        rows = []
        for raw in csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))):
            rows.append(
                {
                    "instrument_token": raw.get("instrument_token", ""),
                    "tradingsymbol": raw.get("tradingsymbol", ""),
                    "name": raw.get("name", ""),
                    "instrument_type": raw.get("instrument_type", ""),
                    "expiry": raw.get("expiry", ""),
                    "strike": raw.get("strike", ""),
                    "tick_size": raw.get("tick_size", ""),
                    "lot_size": raw.get("lot_size", ""),
                    "exchange": raw.get("exchange", ""),
                    "security_key": f"{raw.get('exchange', '')}:{raw.get('tradingsymbol', '')}",
                }
            )
        return rows
