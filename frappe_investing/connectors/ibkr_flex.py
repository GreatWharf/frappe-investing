"""Interactive Brokers Flex Web Service connector.

Two-step fetch: POST ``SendRequest`` with a Flex token + saved query id
returns a ``ReferenceCode``; ``GetStatement`` is then polled with backoff
(max 5 polls, injectable sleeper) until the statement XML is ready. The
Flex query must be created in IBKR Account Management first — this
connector cannot define queries.

* security_key convention: ``ISIN:US0378331005`` when the ISIN is present,
  else ``SMART:{symbol}`` (IBKR routes via SMART; the listing exchange is
  kept in meta).
* The token travels in the query string by IBKR's design; all errors strip
  query strings and additionally replace token occurrences with ``***``.
* XML is parsed with stdlib ElementTree only (no external entity
  processing); the input is capped at 50 MB on top of the 20 MB HTTP cap.
* ``Withholding Tax`` rows are merged into a Dividend's ``taxes`` when a
  dividend with the same date + security exists in the same statement;
  otherwise they become standalone Fee events flagged ``needs_review``.
* Anything unparsed — unknown sections, unknown cash types, malformed rows —
  lands in ``result["errors"]``. Rows are never silently dropped.
"""

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation

from .base import BrokerError, ConnectorBase, http_request, normalized_event, sanitize

SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
MAX_XML_BYTES = 50 * 1024 * 1024  # 50 MB parse cap (HTTP layer caps responses at 20 MB)
MAX_POLLS = 5
_PENDING_CODES = {"100", "101"}  # statement generation queued / in progress

_CORP_TYPE_MAP = {
    "SPLIT": "Split",
    "FS": "Split",
    "REVERSE_SPLIT": "Reverse Split",
    "REVSPLIT": "Reverse Split",
    "RS": "Reverse Split",
    "STOCK_DIVIDEND": "Stock Dividend",
    "SD": "Stock Dividend",
    "SPINOFF": "Spin-off",
    "SO": "Spin-off",
}


def _parse_xml(data):
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_XML_BYTES:
        raise BrokerError(f"Flex XML exceeds {MAX_XML_BYTES} bytes", code="too_large")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise BrokerError("Flex response is not valid XML", code="bad_response") from exc


def _flex_date(text):
    """Flex dates arrive as yyyyMMdd or 'yyyyMMdd;HHMMSS' (sometimes ISO): normalize to ISO."""
    head = (text or "").strip().split(";")[0].split(" ")[0]
    if len(head) == 8 and head.isdigit():
        head = f"{head[:4]}-{head[4:6]}-{head[6:]}"
    return date.fromisoformat(head).isoformat()  # raises ValueError when malformed


def _security_key(attrib):
    isin = (attrib.get("isin") or "").strip()
    if isin:
        return f"ISIN:{isin}"
    return f"SMART:{(attrib.get('symbol') or '').strip()}"


def _abs_str(value):
    if value is None or value == "":
        return "0"
    return str(abs(Decimal(str(value))))


def _row_hash(attrib):
    """Deterministic fallback source_ref when the row carries no id."""
    canonical = json.dumps(sorted(attrib.items()), separators=(",", ":"))
    return hashlib.sha256(f"flex:{canonical}".encode("utf-8")).hexdigest()[:32]


def _map_trade(row, events, errors):
    attrib = row.attrib
    ref = attrib.get("tradeID") or attrib.get("ibOrderID") or _row_hash(attrib)
    try:
        event_type = {"BUY": "Buy", "SELL": "Sell"}[attrib.get("buySell", "").upper()]
        events.append(
            normalized_event(
                event_type,
                security_key=_security_key(attrib),
                qty=_abs_str(attrib["quantity"]),  # Flex sells carry negative quantities
                price=str(Decimal(attrib["tradePrice"])),
                fees=_abs_str(attrib.get("commission") or "0"),  # commissions arrive negative
                currency=attrib.get("currency", ""),
                date=_flex_date(attrib.get("tradeDate") or attrib.get("dateTime")),
                source_ref=ref,
                meta={
                    "symbol": attrib.get("symbol", ""),
                    "exchange": attrib.get("exchange", ""),
                    "asset_category": attrib.get("assetCategory", ""),
                },
            )
        )
    except (KeyError, InvalidOperation, ValueError) as exc:
        errors.append({"section": "Trades", "row": ref, "message": f"skipped trade: {exc}"})


def _map_cash(section, events, errors):
    dividends = {}  # (day, security_key) -> event dict, for WHT merging
    withholding = []
    for row in section:
        attrib = row.attrib
        cash_type = attrib.get("type", "")
        ref = attrib.get("transactionID") or _row_hash(attrib)
        try:
            day = _flex_date(attrib.get("dateTime") or attrib.get("reportDate"))
            amount = Decimal(attrib.get("amount", "0"))
        except (InvalidOperation, ValueError) as exc:
            errors.append({"section": "CashTransactions", "row": ref, "message": f"skipped cash row: {exc}"})
            continue
        security_key = _security_key(attrib) if (attrib.get("symbol") or attrib.get("isin")) else None
        currency = attrib.get("currency", "")
        meta = {"flex_type": cash_type, "description": attrib.get("description", "")}

        if cash_type in {"Dividends", "Payment In Lieu Of Dividends"}:
            if cash_type != "Dividends":
                meta["payment_in_lieu"] = True
            event = normalized_event(
                "Dividend",
                security_key=security_key,
                gross=str(abs(amount)),
                currency=currency,
                date=day,
                source_ref=ref,
                meta=meta,
            )
            dividends[(day, security_key)] = event
            events.append(event)
        elif cash_type == "Withholding Tax":
            withholding.append((day, security_key, currency, abs(amount), ref, meta))
        elif cash_type == "Deposits/Withdrawals":
            events.append(
                normalized_event(
                    "Deposit" if amount >= 0 else "Withdrawal",
                    amount=str(abs(amount)),
                    currency=currency,
                    date=day,
                    source_ref=ref,
                    meta=meta,
                )
            )
        elif cash_type == "Broker Interest Received":
            events.append(
                normalized_event(
                    "Interest", gross=str(abs(amount)), currency=currency, date=day, source_ref=ref, meta=meta
                )
            )
        elif cash_type == "Broker Interest Paid":
            meta["reason"] = "broker interest charged"
            events.append(
                normalized_event(
                    "Fee", amount=str(abs(amount)), currency=currency, date=day, source_ref=ref, meta=meta
                )
            )
        elif cash_type == "Other Fees":
            events.append(
                normalized_event(
                    "Fee", amount=str(abs(amount)), currency=currency, date=day, source_ref=ref, meta=meta
                )
            )
        else:
            errors.append(
                {
                    "section": "CashTransactions",
                    "row": ref,
                    "message": f"unknown cash transaction type {cash_type!r}",
                }
            )
    for day, security_key, currency, tax, ref, meta in withholding:
        dividend = dividends.get((day, security_key))
        if dividend is not None:
            dividend["taxes"] = str(Decimal(dividend["taxes"]) + tax)
        else:
            meta.update(
                {
                    "needs_review": True,
                    "reason": "withholding tax with no matching dividend in this statement",
                }
            )
            events.append(
                normalized_event(
                    "Fee",
                    security_key=security_key,
                    amount=str(tax),
                    currency=currency,
                    date=day,
                    source_ref=ref,
                    meta=meta,
                )
            )


def _map_corporate(row, events, errors):
    attrib = row.attrib
    action_type = (attrib.get("type") or "").upper()
    ref = attrib.get("transactionID") or _row_hash(attrib)
    event_type = _CORP_TYPE_MAP.get(action_type)
    if event_type is None:
        errors.append(
            {
                "section": "CorporateActions",
                "row": ref,
                "message": f"unsupported corporate action type {action_type!r}",
            }
        )
        return
    try:
        day = _flex_date(attrib.get("dateTime") or attrib.get("reportDate"))
    except ValueError as exc:
        errors.append(
            {"section": "CorporateActions", "row": ref, "message": f"skipped corporate action: {exc}"}
        )
        return
    meta = {"flex_type": action_type, "description": attrib.get("description", "")}
    extra = {}
    if event_type in {"Split", "Reverse Split", "Stock Dividend"}:
        ratio = (attrib.get("ratio") or "").strip()
        try:
            extra["split_ratio"] = str(Decimal(ratio)) if ratio else None
        except InvalidOperation:
            extra["split_ratio"] = None
        if extra["split_ratio"] is None:
            meta.update({"needs_review": True, "reason": "ratio missing or unparseable in Flex row"})
    if event_type == "Spin-off":
        meta.update(
            {"needs_review": True, "reason": "basis allocation and child security must be set manually"}
        )
    events.append(
        normalized_event(
            event_type,
            security_key=_security_key(attrib),
            currency=attrib.get("currency", ""),
            date=day,
            source_ref=ref,
            meta=meta,
            **extra,
        )
    )


class IBKRFlexConnector(ConnectorBase):
    """Flex Web Service: trades, cash (dividends/WHT/fees) and corporate actions."""

    name = "ibkr_flex"
    supported = frozenset({"trades", "income", "corporate_actions", "history"})

    def __init__(self, token, session=None, sleeper=time.sleep, poll_interval=2.0, max_polls=MAX_POLLS):
        super().__init__(session=session)
        self.token = token
        self.sleeper = sleeper
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    def fetch(self, query_id):
        """Full two-step fetch: request statement, poll until ready, parse."""
        reference = self.request_statement(query_id)
        return self.parse_statement(self.poll_statement(reference))

    def request_statement(self, query_id):
        """POST SendRequest -> ReferenceCode. The query must already exist in IBKR."""
        body = http_request(
            self.session, "POST", SEND_URL, params={"t": self.token, "q": str(query_id), "v": "3"}
        )
        root = _parse_xml(body)
        if root.tag != "FlexWebServiceResponse":
            raise BrokerError("unexpected Flex SendRequest response", code="bad_response")
        code = root.findtext("ResponseCode", "")
        if code != "0":
            message = sanitize(root.findtext("ResponseText", "") or "request rejected", [self.token])
            raise BrokerError(f"Flex SendRequest failed ({code}): {message}", code="flex_error")
        reference = root.findtext("ReferenceCode", "")
        if not reference:
            raise BrokerError("Flex SendRequest returned no ReferenceCode", code="bad_response")
        return reference

    def poll_statement(self, reference):
        """GET GetStatement with linear backoff until the FlexQueryResponse is ready."""
        for attempt in range(1, self.max_polls + 1):
            body = http_request(
                self.session, "GET", GET_URL, params={"t": self.token, "q": reference, "v": "3"}
            )
            root = _parse_xml(body)
            if root.tag == "FlexQueryResponse":
                return body.decode("utf-8")
            code = root.findtext("ResponseCode", "") if root.tag == "FlexWebServiceResponse" else ""
            text = (root.findtext("ResponseText", "") or "").lower()
            if root.tag == "FlexWebServiceResponse" and (code in _PENDING_CODES or "in progress" in text):
                if attempt < self.max_polls:
                    self.sleeper(self.poll_interval * attempt)
                continue
            message = sanitize(root.findtext("ResponseText", "") or "statement fetch failed", [self.token])
            raise BrokerError(f"Flex GetStatement failed ({code}): {message}", code="flex_error")
        raise BrokerError(f"Flex statement not ready after {self.max_polls} polls", code="timeout")

    @staticmethod
    def parse_statement(xml_text):
        """Parse a FlexQueryResponse into {events, errors}. Nothing is silently dropped."""
        root = _parse_xml(xml_text)
        if root.tag != "FlexQueryResponse":
            raise BrokerError("expected a FlexQueryResponse document", code="bad_response")
        events, errors = [], []
        for statement in root.iter("FlexStatement"):
            for section in statement:
                if section.tag == "Trades":
                    for row in section:
                        _map_trade(row, events, errors)
                elif section.tag == "CashTransactions":
                    _map_cash(section, events, errors)
                elif section.tag == "CorporateActions":
                    for row in section:
                        _map_corporate(row, events, errors)
                else:
                    errors.append(
                        {
                            "section": section.tag,
                            "row": "",
                            "message": f"unparsed Flex section {section.tag!r}",
                        }
                    )
        return {"events": events, "errors": errors}
