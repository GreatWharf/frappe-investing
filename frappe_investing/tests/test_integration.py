"""Run with bench on an isolated test site, never against production accounting data."""

from uuid import uuid4

import frappe

from frappe_investing.services import record_event, value_portfolio

try:
    from frappe.tests import IntegrationTestCase
except ImportError:
    from frappe.tests.utils import FrappeTestCase as IntegrationTestCase


def setup_test_company(company_name, company_abbr):
    """Initialize a dedicated test Company/COA via ERPNext's normal setup stages."""
    if not frappe.flags.in_test or not company_name.startswith("_Test "):
        raise RuntimeError("Test companies may only be initialized in an isolated test context.")
    created = not frappe.db.exists("Company", company_name)
    if created:
        from erpnext.setup.setup_wizard.setup_wizard import setup_complete

        year = frappe.utils.today()[:4]
        setup_complete(
            frappe._dict(
                {
                    "company_name": company_name,
                    "company_abbr": company_abbr,
                    "currency": "USD",
                    "country": "United States",
                    "chart_of_accounts": "Standard",
                    "fy_start_date": f"{year}-01-01",
                    "fy_end_date": f"{year}-12-31",
                }
            )
        )
    if not frappe.db.exists("Company", company_name) or not frappe.db.exists(
        "Account", {"company": company_name, "is_group": 1, "root_type": "Asset"}
    ):
        raise RuntimeError("ERPNext test Company/chart of accounts initialization failed.")
    if created:
        frappe.db.commit()


class TestInvestingIntegration(IntegrationTestCase):
    company = "_Test Investing Feed"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        setup_test_company(cls.company, "TIF")

    def setUp(self):
        super().setUp()
        self.addCleanup(frappe.db.rollback)
        frappe.set_user("Administrator")
        suffix = uuid4().hex[:8]
        self.portfolio = frappe.get_doc(
            {
                "doctype": "Portfolio",
                "portfolio_name": "Test " + suffix,
                "company": self.company,
                "base_currency": "USD",
            }
        ).insert()
        self.account = frappe.get_doc(
            {
                "doctype": "Investment Account",
                "account_name": "Test Account " + suffix,
                "portfolio": self.portfolio.name,
                "currency": "USD",
                "enabled": 1,
            }
        ).insert()
        self.security = frappe.get_doc(
            {
                "doctype": "Security",
                "security_name": "Test Co " + suffix,
                "asset_class": "Stock",
                "currency": "USD",
                "ticker": "TST" + suffix[:4].upper(),
                "status": "Active",
            }
        ).insert()

    def test_csv_preview_flags_unknown_securities(self):
        # Preview must resolve tickers the same way post does: a row preview
        # called valid but post refused (US:AAPL with ticker "AAPL" present,
        # since the convention is full-key tickers like sync_service's).
        from frappe_investing.importer import post_import, preview_import

        header = (
            "date,type,security_key,qty,price,amount,gross,fees,taxes,withholding,currency,"
            "accrued_interest,split_ratio,basis_allocation,child_security,child_ratio,"
            "lot_ids,target_currency,target_amount,notes,source_ref\n"
        )
        bad = header + "2026-01-05,Buy,US:NOPE9,1,10,,,,,,USD,,,,,,,,,x,IMP-X1\n"
        out = preview_import(bad, account=self.account.name)
        self.assertEqual(out["valid_rows"], 0)
        self.assertEqual(out["error_rows"], 1)
        self.assertIn("Unknown security", out["errors"][0]["message"])
        with self.assertRaises(Exception):
            post_import(bad, account=self.account.name)

        good = header + f"2026-01-05,Buy,US:{self.security.ticker},1,10,,,,,,USD,,,,,,,,,x,IMP-X2\n"
        frappe.get_doc("Security", self.security.name).db_set("ticker", f"US:{self.security.ticker}")
        out = preview_import(good, account=self.account.name)
        self.assertEqual(out["valid_rows"], 1)

    def test_license_key_round_trips_and_unlocks_pro(self):
        # license_key is a Password field: _resolve read it back with
        # doc.get(), fed the encrypted blob to evaluate(), and every status
        # call reported "invalid" right after a successful save.
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from frappe_investing import license_service, licensing

        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        original = licensing.PUBLIC_KEY
        licensing.PUBLIC_KEY = base64.urlsafe_b64encode(public).decode()
        try:
            key = licensing.sign_license(
                {"product": licensing.PRODUCT, "tier": "pro", "customer": "Test Co", "expires": "2099-01-01"},
                private,
            )
            saved = license_service.save_license(key)
            self.assertEqual(saved["tier"], "pro")
            # The saved key must survive a fresh read, not just the save call.
            self.assertEqual(license_service.public_state()["tier"], "pro")
        finally:
            licensing.PUBLIC_KEY = original

    def test_get_dashboard_tolerates_form_encoded_risk_free_rate(self):
        # The desk page calls this over HTTP, where arguments arrive as
        # strings; flt() turned risk_free_rate into a float and the money
        # guard then rejected the whole dashboard payload with a TypeError.
        from frappe_investing import api

        record_event(
            {
                "event_type": "Deposit",
                "posting_date": "2026-01-02",
                "account": self.account.name,
                "amount": "1000",
                "currency": "USD",
                "source": "Manual",
            }
        )
        payload = api.get_dashboard(self.portfolio.name, risk_free_rate="0.04")
        self.assertEqual(str(payload["performance"]["risk_free_rate"]), "0.04")
        self.assertEqual(payload["portfolio"], self.portfolio.name)

    def test_buy_sell_creates_lots_allocations_and_realized_pnl(self):
        buy, created = record_event(
            {
                "event_type": "Buy",
                "posting_date": "2026-01-05",
                "account": self.account.name,
                "security": self.security.name,
                "qty": "100",
                "price": "10",
                "currency": "USD",
                "source": "Manual",
            }
        )
        self.assertTrue(created)
        lots = frappe.get_all(
            "Tax Lot", filters={"security": self.security.name}, fields=["qty_open", "unit_cost", "status"]
        )
        self.assertEqual(len(lots), 1)
        sell, _ = record_event(
            {
                "event_type": "Sell",
                "posting_date": "2026-02-01",
                "account": self.account.name,
                "security": self.security.name,
                "qty": "40",
                "price": "15",
                "currency": "USD",
                "source": "Manual",
            }
        )
        allocations = frappe.get_all("Lot Allocation", filters={"event": sell}, pluck="realized_pnl")
        self.assertEqual(len(allocations), 1)
        self.assertAlmostEqual(float(allocations[0]), 200.0)

    def test_idempotent_replay_and_valuation(self):
        data = {
            "event_type": "Buy",
            "posting_date": "2026-01-05",
            "account": self.account.name,
            "security": self.security.name,
            "qty": "10",
            "price": "10",
            "currency": "USD",
            "source": "Zerodha",
            "source_ref": "trade-test-1",
        }
        first, c1 = record_event(dict(data))
        second, c2 = record_event(dict(data))
        self.assertEqual(first, second)
        self.assertFalse(c2)
        # Security Price is engine-managed; the test writes one the way the
        # engine does — behind the investing_internal flag.
        frappe.flags.investing_internal = True
        try:
            frappe.get_doc(
                {
                    "doctype": "Security Price",
                    "security": self.security.name,
                    "date": "2026-02-01",
                    "close": "12.5",
                    "currency": "USD",
                    "source": "Manual",
                }
            ).insert()
        finally:
            frappe.flags.investing_internal = False
        result = value_portfolio(self.portfolio.name, day="2026-02-01")
        self.assertEqual(float(result["total_value"]), 125.0)

    def test_free_tier_blocks_a_second_asset_class(self):
        # The fixture already created a Stock security; the free tier covers 1 class.
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(
                {
                    "doctype": "Security",
                    "security_name": "Test Coin",
                    "asset_class": "Crypto",
                    "currency": "USD",
                    "status": "Active",
                }
            ).insert()

    def test_free_tier_allows_more_of_the_same_class(self):
        # Adding another Stock does not grow the class count beyond the limit.
        frappe.get_doc(
            {
                "doctype": "Security",
                "security_name": "Another Stock",
                "asset_class": "Stock",
                "currency": "USD",
                "status": "Active",
            }
        ).insert()
