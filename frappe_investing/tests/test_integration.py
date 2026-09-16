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
        result = value_portfolio(self.portfolio.name, day="2026-02-01")
        self.assertEqual(float(result["total_value"]), 125.0)

    def test_crypto_requires_pro_license(self):
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
