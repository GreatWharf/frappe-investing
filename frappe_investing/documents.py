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
        if self.doctype in self.INTERNAL_SAFE:
            # Engine records are insert-only from Desk: direct updates to
            # engine-owned fields (cost basis, valuations) would silently
            # rewrite history, so they need the same internal flag as inserts.
            protected = {
                "Tax Lot": {"qty_open", "qty_original", "unit_cost", "status", "engine_id"},
                "Lot Allocation": {"qty", "cost", "proceeds", "realized_pnl"},
                "Portfolio Snapshot": {"total_value", "total_cost", "unrealized_pnl"},
                "Security Price": {"close"},
                "FX Rate": {"rate"},
                "Broker Sync Log": {"status", "events_created", "positions_seen", "prices_seen"},
                "Import Batch": {"status", "imported", "failed"},
            }.get(self.doctype, set())
            changed = {f for f in protected if self.has_value_changed(f)}
            if changed:
                frappe.throw(
                    f"{self.doctype} field(s) {', '.join(sorted(changed))} are written by the "
                    "investing engine; direct Desk edits are not allowed.",
                    frappe.PermissionError,
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
        # The gate runs on every save, not just insert: changing asset_class
        # on an existing row, or reactivating a Delisted row, counts as
        # newly tracking that class, so it must pass the tier check too.
        if (
            self.asset_class
            and self.asset_class != "Benchmark"
            and (self.is_new() or self._retracks_class())
        ):
            from .license_service import require_asset_class

            require_asset_class(self.asset_class)
        if self.asset_class == "Bond":
            for f in ("face_value", "coupon_rate", "coupon_frequency", "issue_date", "maturity_date"):
                if not self.get(f):
                    frappe.throw(f"Bonds require {f}.")
            if self.issue_date >= self.maturity_date:
                frappe.throw("Maturity must be after issue.")

    def _retracks_class(self):
        """True when this save starts tracking a class the row did not track before."""
        try:
            before = self.get_doc_before_save()
        except Exception:
            before = None
        if before is None:
            return self.asset_class != "Benchmark" and bool(
                self.has_value_changed("asset_class") or self.has_value_changed("status")
            )
        if self.asset_class == "Benchmark":
            return False
        return self.asset_class != before.get("asset_class") or (
            before.get("status") == "Delisted" and self.status == "Active"
        )

    # -- Investment Event ---------------------------------------------------
    def _validate_event(self):
        if self.is_new():
            self._event_order_guard()
            self._compute_dedupe()
            self._validate_transfer()
        event = self.to_core_event()
        event.validate()
        if self.security:
            asset_class = frappe.db.get_value("Security", self.security, "asset_class")
            if asset_class == "Benchmark":
                # Benchmark securities are a price catalog, not holdings:
                # they hold no lots, so no holdings-side event may post
                # against them (compare_benchmark reads their prices only).
                frappe.throw(
                    "Benchmark securities hold no positions; post events against a real holding.",
                    frappe.PermissionError,
                )
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

    def _validate_transfer(self):
        """Transfer Out names its destination; Transfer In is engine-chained.

        Users submit a Transfer Out with a target_account. The matching
        Transfer In is created by the engine (_chain_transfer_in), never by
        hand — a hand-built In has no carried lots and can never submit.
        """
        if self.event_type == "Transfer Out":
            if not self.get("target_account"):
                frappe.throw("Transfer Out requires a target account.")
            if self.get("target_account") == self.account:
                frappe.throw("Transfer Out target must differ from the source account.")
        elif self.event_type == "Transfer In":
            if not self.get("transfer_ref") and not frappe.flags.get("investing_rebuild"):
                frappe.throw(
                    "Transfer In is created automatically from its Transfer Out; "
                    "submit the Transfer Out instead."
                )

    def _compute_dedupe(self):
        if self.source == "Manual" and not self.source_ref:
            return
        import hashlib

        # Content-aware: broker amendments (same ref, corrected qty/price)
        # hash differently from the original, so corrections are never
        # silently dropped as duplicates.
        raw = "|".join(
            str(x or "")
            for x in (
                self.account,
                self.source,
                self.source_ref,
                self.event_type,
                self.get("qty"),
                self.get("price"),
                self.get("amount"),
                self.get("gross"),
            )
        )
        self.dedupe_key = hashlib.sha256(raw.encode()).hexdigest()[:40]

    def _resolve_lot_ids(self):
        """Translate user-entered Tax Lot names to engine ids for SPECIFIC sells.

        Users only ever see Tax Lot document names (engine_id is hidden); the
        core engine matches on Lot.id == Tax Lot engine_id. Pass engine ids
        through untouched so API callers can keep using them.
        """
        raw = tuple(x.strip() for x in (self.lot_ids or "").split(",") if x.strip())
        if not raw:
            return raw
        resolved = []
        for name_or_id in raw:
            engine_id = frappe.db.get_value("Tax Lot", {"engine_id": name_or_id}, "engine_id")
            if engine_id:
                resolved.append(engine_id)
                continue
            row_id = frappe.db.get_value(
                "Tax Lot",
                {
                    "name": name_or_id,
                    "account": self.account,
                    "security": self.security,
                    "status": "Open",
                },
                "engine_id",
            )
            resolved.append(row_id or name_or_id)
        return tuple(resolved)

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
            lot_ids=self._resolve_lot_ids(),
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
