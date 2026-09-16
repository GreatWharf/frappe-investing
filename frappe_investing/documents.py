"""Document controllers: guard managed records, enforce tier gates and event integrity."""

import frappe
from frappe.model.document import Document

from .core.events import Event
from .core.money import dec


class ManagedDocument(Document):
    """Internal bookkeeping writes require the server-side investing_internal flag."""

    INTERNAL_SAFE = {
        "Tax Lot",
        "Lot Allocation",
        "Portfolio Snapshot",
        "Broker Sync Log",
        "Security Price",
        "FX Rate",
        "Import Batch",
        "Investment License",
    }

    def _guard(self):
        if frappe.flags.get("investing_internal"):
            return
        if self.doctype in self.INTERNAL_SAFE and self.is_new():
            frappe.throw(
                f"{self.doctype} records are written by the investing engine.", frappe.PermissionError
            )

    def validate(self):
        self._guard()
        if self.doctype == "Security":
            self._validate_security()
        elif self.doctype == "Investment Event":
            self._validate_event()
        elif self.doctype == "Investment Accounting Policy":
            self._validate_policy()
        elif self.doctype == "Broker Connection":
            self._validate_connection()

    def on_trash(self):
        if self.doctype in self.INTERNAL_SAFE:
            frappe.throw(
                f"{self.doctype} records are retained for audit; they cannot be deleted individually."
            )

    # -- Security ---------------------------------------------------------
    def _validate_security(self):
        if self.is_new() and self.asset_class:
            from .license_service import require_asset_class

            require_asset_class(self.asset_class)
        if self.asset_class == "Bond":
            for f in ("face_value", "coupon_rate", "coupon_frequency", "issue_date", "maturity_date"):
                if not self.get(f):
                    frappe.throw(f"Bonds require {f}.")
            if self.issue_date >= self.maturity_date:
                frappe.throw("Maturity must be after issue.")

    # -- Investment Event ---------------------------------------------------
    def _validate_event(self):
        if self.is_new():
            self._event_order_guard()
            self._compute_dedupe()
        event = self.to_core_event()
        event.validate()
        if self.security:
            asset_class = frappe.db.get_value("Security", self.security, "asset_class")
            if asset_class == "Bond" and self.event_type in {"Buy", "Sell"}:
                # Bond accounting must show accrued interest explicitly.
                if self.get("accrued_interest") in (None, ""):
                    self.accrued_interest = "0"

    def _event_order_guard(self):
        if self.reversal_of:
            return
        later = frappe.db.get_value(
            "Investment Event",
            {
                "account": self.account,
                "security": self.security,
                "docstatus": 1,
                "posting_date": [">", self.posting_date],
            },
            "name",
        )
        if later and not frappe.flags.get("investing_rebuild"):
            frappe.throw(
                f"Later event {later} already exists for this security/account. "
                "Cancel later events first or ask a manager to rebuild positions."
            )

    def _compute_dedupe(self):
        if self.source == "Manual" and not self.source_ref:
            return
        import hashlib

        raw = "|".join(str(x or "") for x in (self.account, self.source, self.source_ref))
        self.dedupe_key = hashlib.sha256(raw.encode()).hexdigest()[:40]

    def to_core_event(self):
        def d(field):
            value = self.get(field)
            return None if value in (None, "") else dec(str(value))

        import json

        return Event(
            type=self.event_type,
            date=self.posting_date,
            account=self.account,
            currency=self.currency,
            security=self.security,
            qty=d("qty"),
            price=d("price"),
            amount=d("amount"),
            gross=d("gross"),
            fees=d("fees") or dec(0),
            taxes=d("taxes") or dec(0),
            accrued_interest=d("accrued_interest") or dec(0),
            split_ratio=d("split_ratio"),
            basis_allocation=d("basis_allocation"),
            child_ratio=d("child_ratio"),
            child_security=self.child_security,
            target_currency=self.target_currency,
            target_amount=d("target_amount"),
            lot_ids=tuple(x.strip() for x in (self.lot_ids or "").split(",") if x.strip()),
            source=self.source,
            source_ref=self.source_ref or "",
            notes=self.notes or "",
            meta=json.loads(self.meta_json or "{}"),
        )

    def on_submit(self):
        from .services import apply_event

        apply_event(self)

    def on_cancel(self):
        from .services import reverse_event

        reverse_event(self)

    # -- Accounting policy --------------------------------------------------
    def _validate_policy(self):
        seen = set()
        for row in self.mappings:
            if row.event_type in seen:
                frappe.throw(f"Duplicate accounting rule for {row.event_type}.")
            seen.add(row.event_type)
            for account_field in (
                "debit_account",
                "credit_account",
                "tax_debit_account",
                "fee_debit_account",
                "pnl_account",
            ):
                account = row.get(account_field)
                if account and frappe.db.get_value("Account", account, "company") != self.company:
                    frappe.throw(f"{account} does not belong to {self.company}.")

    # -- Broker connection ---------------------------------------------------
    def _validate_connection(self):
        if self.broker == "Zerodha":
            if not self.api_key or not self.get_password("api_secret", raise_exception=False):
                frappe.throw("Zerodha requires an API key and secret from the Kite developer console.")
        elif self.broker == "Alpaca":
            if not self.api_key or not self.get_password("api_secret", raise_exception=False):
                frappe.throw("Alpaca requires an API key and secret.")
        elif self.broker == "Interactive Brokers":
            if not self.get_password("flex_token", raise_exception=False) or not self.flex_query_id:
                frappe.throw("IBKR requires a Flex Web Service token and saved-query ID.")
        if self.enabled and self.status in {"Not Connected", "Token Expired"} and self.broker != "CSV Import":
            frappe.throw("Connect the broker successfully before enabling sync.")
