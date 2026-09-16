"""Generic CSV event import.

``parse_csv`` is a pure function: it never writes anything. It returns the
full parsed event list plus the full error list — the caller must treat
``errors`` as authoritative and commit only when it is empty. Never commit
a partial parse. Errors report the file line number (header = line 1, first
data row = line 2).

Default column layout (header row required)::

    date,type,security_key,qty,price,amount,gross,fees,taxes,currency,
    split_ratio,basis_allocation,child_security,child_ratio,notes,source_ref

``mapping`` renames incoming headers to these canonical fields ({"Trade
Date": "date", ...}) so broker exports import without editing the file.
Columns that do not map to a canonical field are ignored.

* ``date`` is strict YYYY-MM-DD; ``type`` must be a core EVENT_TYPES member.
* qty/price/amount/gross/fees/taxes stay strings in the output — no float
  ever crosses the boundary — and every non-empty one must parse as Decimal.
* Rows are validated by constructing a core ``Event`` (security_key ->
  security, account supplied by the caller) and running ``validate()``;
  failures land in ``errors`` with the core message.
* A missing ``source_ref`` is filled with a deterministic
  sha256("csv:" + canonical row JSON)[:32], so re-importing the same file
  is idempotent.
"""

import csv
import hashlib
import io
import json
from datetime import date
from decimal import InvalidOperation

from ..core import events as core_events
from ..core.money import dec

DEFAULT_FIELDS = (
    "date",
    "type",
    "security_key",
    "qty",
    "price",
    "amount",
    "gross",
    "fees",
    "taxes",
    "currency",
    "split_ratio",
    "basis_allocation",
    "child_security",
    "child_ratio",
    "notes",
    "source_ref",
)
NUMERIC_FIELDS = frozenset(
    {"qty", "price", "amount", "gross", "fees", "taxes", "split_ratio", "basis_allocation", "child_ratio"}
)


def _row_ref(raw):
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"csv:{canonical}".encode("utf-8")).hexdigest()[:32]


def _translate(raw, line_no, account, errors):
    """One canonical row dict -> normalized event dict, or None after recording an error."""

    def fail(message):
        errors.append({"row": line_no, "message": message})

    event_type = raw.get("type", "")
    if event_type not in core_events.EVENT_TYPES:
        fail(f"Unknown event type {event_type!r}")
        return None
    try:
        day = date.fromisoformat(raw.get("date", ""))
    except ValueError:
        fail(f"Invalid date {raw.get('date', '')!r} (want YYYY-MM-DD)")
        return None
    currency = raw.get("currency", "")
    if not currency:
        fail("currency is required")
        return None

    numbers = {}
    for field in sorted(NUMERIC_FIELDS):
        value = raw.get(field, "")
        if value == "":
            numbers[field] = None
            continue
        try:
            dec(value)
        except (InvalidOperation, TypeError, ValueError):
            fail(f"Field {field} is not a valid decimal: {value!r}")
            return None
        numbers[field] = value

    source_ref = raw.get("source_ref", "") or _row_ref(raw)
    event = {
        "type": event_type,
        "security_key": raw.get("security_key") or None,
        "qty": numbers["qty"],
        "price": numbers["price"],
        "amount": numbers["amount"],
        "gross": numbers["gross"],
        "fees": numbers["fees"] or "0",
        "taxes": numbers["taxes"] or "0",
        "currency": currency,
        "date": day.isoformat(),
        "source_ref": source_ref,
        "meta": {"csv_row": line_no},
    }
    for optional in ("split_ratio", "basis_allocation", "child_ratio"):
        if numbers[optional] is not None:
            event[optional] = numbers[optional]
    if raw.get("child_security"):
        event["child_security"] = raw["child_security"]
    if raw.get("notes"):
        event["notes"] = raw["notes"]

    def parsed(field):
        return dec(numbers[field]) if numbers[field] else None

    try:
        core_events.Event(
            type=event_type,
            date=day,
            account=account,
            currency=currency,
            security=event["security_key"],
            qty=parsed("qty"),
            price=parsed("price"),
            amount=parsed("amount"),
            gross=parsed("gross"),
            fees=dec(event["fees"]),
            taxes=dec(event["taxes"]),
            split_ratio=parsed("split_ratio"),
            basis_allocation=parsed("basis_allocation"),
            child_security=raw.get("child_security") or None,
            child_ratio=parsed("child_ratio"),
            source="CSV",
            source_ref=source_ref,
            notes=raw.get("notes", ""),
            meta=event["meta"],
        ).validate()
    except (ValueError, TypeError) as exc:
        fail(str(exc))
        return None
    return event


def parse_csv(text, mapping=None, account="Imported"):
    """Parse CSV text into {"events": [...], "errors": [{"row", "message"}]}.

    ``mapping`` maps incoming header names to canonical field names.
    ``account`` is the target account name used for core validation.
    """
    mapping = dict(mapping or {})
    events_out, errors = [], []
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return {"events": [], "errors": [{"row": 0, "message": "empty CSV"}]}
    fields = [mapping.get(column.strip(), column.strip()) for column in header]
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        raw = {}
        for index, field in enumerate(fields):
            if field in DEFAULT_FIELDS:
                raw[field] = (row[index] if index < len(row) else "").strip()
        event = _translate(raw, line_no, account, errors)
        if event is not None:
            events_out.append(event)
    return {"events": events_out, "errors": errors}
