"""Statement CSV import orchestration: preview counts, per-row errors, posting.

Thin layer over connectors.csv_import.parse_csv:

* ``preview_import`` parses and counts only — it never writes anything.
* ``post_import`` re-parses and refuses to post when any row has an error
  (never a partial commit), then posts every valid row through
  ``services.record_event`` so the dedupe/idempotency path in services.py
  applies unchanged.
* ``cost_method_info`` surfaces the portfolio cost-method selector backing
  (portfolio override or settings default) for the import flow.

Ticker resolution mirrors sync_service._resolve_security: exact ticker
first, then ISIN: prefix — but never auto-creates (no license/asset-class
side effects from a preview path; unknown tickers are row errors).
"""

import frappe

from . import services
from .connectors import csv_import


class ImportError(ValueError):
    """Refusal to post. Carries per-row ``errors`` so the caller can persist them."""

    def __init__(self, message, errors=()):
        super().__init__(message)
        self.errors = list(errors)


# Supported-operations reference for the import dialog's second tab. Required
# lists mirror core/events.py validate(); base columns (date, type, currency)
# apply to every row and are listed once in the UI heading.
OPERATIONS_REFERENCE = [
    {"type": "Buy", "required": ["security_key", "qty", "price"], "optional": ["fees", "taxes", "accrued_interest", "notes", "source_ref"]},
    {"type": "Sell", "required": ["security_key", "qty", "price"], "optional": ["fees", "taxes", "lot_ids", "notes", "source_ref"]},
    {"type": "Dividend", "required": ["security_key", "gross"], "optional": ["taxes / withholding", "fees", "notes", "source_ref"]},
    {"type": "Coupon", "required": ["security_key", "gross"], "optional": ["taxes / withholding", "fees", "notes", "source_ref"]},
    {"type": "Interest", "required": ["gross"], "optional": ["taxes / withholding", "fees", "notes", "source_ref"]},
    {"type": "Fee", "required": ["amount"], "optional": ["notes", "source_ref"]},
    {"type": "Deposit", "required": ["amount"], "optional": ["notes", "source_ref"]},
    {"type": "Withdrawal", "required": ["amount"], "optional": ["notes", "source_ref"]},
    {"type": "Transfer In", "required": ["security_key", "qty"], "optional": ["notes", "source_ref"]},
    {"type": "Transfer Out", "required": ["security_key", "qty"], "optional": ["notes", "source_ref"]},
    {"type": "Split", "required": ["security_key", "split_ratio above 1"], "optional": ["notes", "source_ref"]},
    {"type": "Reverse Split", "required": ["security_key", "split_ratio below 1"], "optional": ["notes", "source_ref"]},
    {"type": "Stock Dividend", "required": ["security_key", "split_ratio"], "optional": ["notes", "source_ref"]},
    {"type": "Spin-off", "required": ["security_key", "child_security", "basis_allocation (0–1)", "child_ratio"], "optional": ["notes", "source_ref"]},
    {"type": "Cash-in-lieu", "required": ["security_key", "qty", "price"], "optional": ["notes", "source_ref"]},
    {"type": "Redemption", "required": ["security_key", "qty", "price"], "optional": ["notes", "source_ref"]},
    {"type": "FX Conversion", "required": ["amount", "target_currency", "target_amount"], "optional": ["notes", "source_ref"]},
]


def _account_or_throw(account):
    try:
        doc = frappe.get_doc("Investment Account", account)
    except Exception:
        raise ImportError(f"Unknown or disabled Investment Account: {account}.")
    if not doc or doc.get("doctype") != "Investment Account" or not doc.get("enabled"):
        raise ImportError(f"Unknown or disabled Investment Account: {account}.")
    return doc


def _resolve_security(security_key):
    """Ticker (then ISIN:) lookup. Unknown keys are row errors, never created."""
    if not security_key:
        return None
    existing = frappe.db.get_value("Security", {"ticker": security_key}, "name")
    if existing:
        return existing
    if security_key.startswith("ISIN:"):
        existing = frappe.db.get_value("Security", {"isin": security_key[5:]}, "name")
        if existing:
            return existing
    return None


def _event_to_doc(account, event):
    """Map a parsed connector event to Investment Event fields (sync_service shape)."""
    security = _resolve_security(event.get("security_key"))
    if event.get("security_key") and not security:
        raise ImportError(f"Unknown security {event['security_key']!r}: create the Security first.")
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
        "accrued_interest": event.get("accrued_interest"),
        "currency": event["currency"],
        "source": "CSV Import",
        "source_ref": event.get("source_ref"),
        "split_ratio": event.get("split_ratio"),
        "basis_allocation": event.get("basis_allocation"),
        "child_security": event.get("child_security"),
        "child_ratio": event.get("child_ratio"),
        "lot_ids": event.get("lot_ids"),
        "target_currency": event.get("target_currency"),
        "target_amount": event.get("target_amount"),
        "meta_json": frappe.as_json(event.get("meta") or {}),
        "notes": event.get("notes", ""),
    }


def preview_import(csv_text, *, account, mapping=None):
    """Parse only. Returns counts, per-type breakdown, events and row errors."""
    _account_or_throw(account)
    parsed = csv_import.parse_csv(csv_text, mapping=mapping, account=account)
    events = parsed["events"]
    by_type = {}
    for event in events:
        by_type[event["type"]] = by_type.get(event["type"], 0) + 1
    total_rows = len(events) + len(parsed["errors"])
    return {
        "account": account,
        "total_rows": total_rows,
        "valid_rows": len(events),
        "error_rows": len(parsed["errors"]),
        "by_type": by_type,
        "events": events,
        "errors": parsed["errors"],
    }


def post_import(csv_text, *, account, mapping=None):
    """Validate everything first, then post every row via services.record_event.

    Raises ImportError (carrying the per-row ``errors``) while any row is
    invalid — never a partial commit. Unknown securities surface as row
    errors here too, attached to their CSV line number.
    """
    _account_or_throw(account)
    parsed = csv_import.parse_csv(csv_text, mapping=mapping, account=account)
    if parsed["errors"]:
        first = parsed["errors"][0]
        raise ImportError(
            f"Fix {len(parsed['errors'])} row error(s) before posting "
            f"(first: row {first['row']}: {first['message']}).",
            parsed["errors"],
        )
    posted, created = 0, 0
    errors = []
    for event in parsed["events"]:
        try:
            _name, was_created = services.record_event(_event_to_doc(account, event))
        except ImportError as exc:
            errors.append({"row": (event.get("meta") or {}).get("csv_row"), "message": str(exc)})
            continue
        posted += 1
        created += int(bool(was_created))
    if errors:
        raise ImportError(
            f"{len(errors)} row(s) could not be posted (first: row "
            f"{errors[0]['row']}: {errors[0]['message']}).",
            errors,
        )
    return {"account": account, "posted": posted, "created": created, "errors": []}


def cost_method_info(account):
    """The capital-gains cost-method backing for the import flow's selector."""
    doc = _account_or_throw(account)
    portfolio = doc.get("portfolio")
    override = frappe.db.get_value("Portfolio", portfolio, "cost_method") if portfolio else None
    return {
        "portfolio": portfolio,
        "portfolio_cost_method": override,
        "effective": services.cost_method_for(portfolio) if portfolio else None,
    }
