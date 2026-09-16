"""Alpaca connector (US equities, cash accounts).

* Base URL is https://api.alpaca.markets (live). ``sandbox=True`` switches
  to https://paper-api.alpaca.markets; the product default is live —
  "sandbox default off" — so pass ``sandbox=True`` explicitly for paper.
* security_key convention: ``US:AAPL``. Alpaca symbols do not carry an
  exchange segment, so the exchange position is the literal ``US``.
* Splits: Alpaca SPLIT activities report only the *additional* shares
  credited, so the new/old ratio is not derivable without the prior
  position. Such events are emitted with ``split_ratio=None`` and
  ``meta={"needs_review": True, ...}`` — set the ratio manually before
  submitting.
* Withholding (DIVWHT) arrives as an activity separate from the dividend;
  it is emitted as a Fee flagged ``needs_review`` rather than guessed-joined.
* Unknown activity types are never dropped silently: they come back in the
  result's ``skipped`` list with a reason.
* ``activities()`` paginates via ``page_token`` -> ``next_page_token`` with
  a hard cap of 50 pages; hitting the cap sets ``truncated=True``.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from .base import ConnectorBase, http_json, normalized_event

LIVE_BASE = "https://api.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"
MAX_PAGES = 50

_DIVIDEND_TYPES = {"DIV", "DIVNRA", "DIVROC", "DIVTW", "DIVCGL", "DIVCGS"}


def _abs_str(value):
    """Absolute value of a broker amount, as a Decimal-parseable string."""
    if value is None or value == "":
        return "0"
    return str(abs(Decimal(str(value))))


def _is_negative(value):
    try:
        return Decimal(str(value)) < 0
    except (InvalidOperation, TypeError):
        return False


def _map_activity(row):
    """One activity -> (event, None) or (None, reason). Best-effort with honest meta flags."""
    kind = row.get("activity_type", "")
    symbol = row.get("symbol") or ""
    security_key = f"US:{symbol}" if symbol else None
    day = str(row.get("transaction_time") or row.get("date") or "")[:10]
    source_ref = str(row.get("id", ""))
    amount = row.get("net_amount")
    meta = {"activity_type": kind}

    if kind == "FILL":
        side = str(row.get("side", "")).lower()
        event_type = {"buy": "Buy", "sell": "Sell"}.get(side)
        if event_type is None:
            return None, f"FILL with unknown side {side!r}"
        meta["order_id"] = str(row.get("order_id", ""))
        return normalized_event(
            event_type,
            security_key=security_key,
            qty=str(row.get("qty", "0")),
            price=str(row.get("price", "0")),
            currency="USD",
            date=day,
            source_ref=source_ref,
            meta=meta,
        ), None
    if kind in _DIVIDEND_TYPES:
        return normalized_event(
            "Dividend",
            security_key=security_key,
            gross=str(amount or "0"),
            currency="USD",
            date=day,
            source_ref=source_ref,
            meta=meta,
        ), None
    if kind == "DIVWHT":
        meta.update(
            {
                "needs_review": True,
                "symbol": symbol,
                "reason": "dividend withholding; merge into the matching Dividend's taxes",
            }
        )
        return normalized_event(
            "Fee",
            security_key=security_key,
            amount=_abs_str(amount),
            currency="USD",
            date=day,
            source_ref=source_ref,
            meta=meta,
        ), None
    if kind == "SPLIT":
        meta.update(
            {
                "needs_review": True,
                "additional_shares": str(row.get("qty", "")),
                "reason": "old quantity unknown; split ratio not derivable from the activity",
            }
        )
        return normalized_event(
            "Split",
            security_key=security_key,
            split_ratio=None,
            currency="USD",
            date=day,
            source_ref=source_ref,
            meta=meta,
        ), None
    if kind == "SPIN":
        meta.update(
            {"needs_review": True, "reason": "basis allocation and child ratio are not provided by Alpaca"}
        )
        return normalized_event(
            "Spin-off", security_key=security_key, currency="USD", date=day, source_ref=source_ref, meta=meta
        ), None
    if kind == "CSD":
        return normalized_event(
            "Deposit", amount=_abs_str(amount), currency="USD", date=day, source_ref=source_ref, meta=meta
        ), None
    if kind == "CSW":
        return normalized_event(
            "Withdrawal", amount=_abs_str(amount), currency="USD", date=day, source_ref=source_ref, meta=meta
        ), None
    if kind == "INT":
        meta["direction"] = "paid" if _is_negative(amount) else "received"
        return normalized_event(
            "Interest", gross=_abs_str(amount), currency="USD", date=day, source_ref=source_ref, meta=meta
        ), None
    if kind == "FEE":
        return normalized_event(
            "Fee", amount=_abs_str(amount), currency="USD", date=day, source_ref=source_ref, meta=meta
        ), None
    return None, f"unsupported activity type {kind!r}"


class AlpacaConnector(ConnectorBase):
    """Alpaca v2: account, positions and account activities (fills, dividends, splits)."""

    name = "alpaca"
    supported = frozenset({"accounts", "positions", "trades", "income", "corporate_actions", "history"})

    def __init__(self, api_key, secret_key, sandbox=False, session=None):
        super().__init__(session=session)
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = PAPER_BASE if sandbox else LIVE_BASE

    def _headers(self):
        return {"APCA-API-KEY-ID": self.api_key, "APCA-API-SECRET-KEY": self.secret_key}

    def _get(self, path, params=None):
        return http_json(
            self.session, "GET", f"{self.base_url}{path}", headers=self._headers(), params=params
        )

    def account(self):
        """GET /v2/account -> {external_id, name, currency}."""
        payload = self._get("/v2/account")
        number = str(payload.get("account_number") or payload.get("id") or "")
        return {"external_id": number, "name": f"Alpaca {number}", "currency": payload.get("currency", "USD")}

    def positions(self):
        """GET /v2/positions -> normalized position dicts (USD)."""
        positions = []
        for row in self._get("/v2/positions") or []:
            avg_cost = row.get("avg_entry_price")
            current = row.get("current_price")
            positions.append(
                {
                    "security_key": f"US:{row.get('symbol', '')}",
                    "qty": str(row.get("qty", "0")),
                    "avg_cost": str(avg_cost) if avg_cost is not None else None,
                    "currency": "USD",
                    "market_price": str(current) if current is not None else None,
                    "as_of": date.today().isoformat(),
                }
            )
        return positions

    def activities(self, after=None, until=None, page_token=None):
        """GET /v2/account/activities -> {events, skipped, pages, truncated}.

        Fills become Buy/Sell, dividends Dividend (gross=net_amount), splits
        Split with needs_review (see module docstring), cash movements
        Deposit/Withdrawal/Interest/Fee. Unknown types land in ``skipped``
        with a reason.
        """
        events, skipped = [], []
        token, pages, truncated = page_token, 0, False
        while True:
            if pages >= MAX_PAGES:
                truncated = True
                break
            params = {"page_size": 100}
            if after:
                params["after"] = str(after)
            if until:
                params["until"] = str(until)
            if token:
                params["page_token"] = token
            payload = self._get("/v2/account/activities", params=params)
            pages += 1
            if isinstance(payload, dict):
                rows = payload.get("activities", [])
                token = payload.get("next_page_token")
            else:  # bare array: Alpaca's unpaginated form — no next token derivable
                rows, token = payload, None
            for row in rows or []:
                event, reason = _map_activity(row)
                if event is not None:
                    events.append(event)
                else:
                    skipped.append(
                        {
                            "id": row.get("id", ""),
                            "activity_type": row.get("activity_type", ""),
                            "reason": reason,
                        }
                    )
            if not token:
                break
        return {"events": events, "skipped": skipped, "pages": pages, "truncated": truncated}
