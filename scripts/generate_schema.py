"""Generate the declarative app schema; refuses to overwrite existing files."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "frappe_investing"
MODULE = "Investing"


def field(name, kind="Data", **options):
    return {"fieldname": name, "label": name.replace("_", " ").title(), "fieldtype": kind, **options}


def link(name, target, **options):
    return field(name, "Link", options=target, **options)


def select(name, values, **options):
    return field(name, "Select", options="\n".join(values), **options)


def section(name):
    return field(name, "Section Break")


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"Refusing to overwrite {path}")
    path.write_text(content)


EVENT_TYPES = [
    "Buy",
    "Sell",
    "Dividend",
    "Coupon",
    "Interest",
    "Fee",
    "Deposit",
    "Withdrawal",
    "Transfer In",
    "Transfer Out",
    "Split",
    "Reverse Split",
    "Stock Dividend",
    "Spin-off",
    "Cash-in-lieu",
    "Redemption",
    "FX Conversion",
]

USER = {"role": "Investment User", "read": 1, "create": 1, "write": 1}
MANAGER = {"role": "Investment Manager", "read": 1, "create": 1, "write": 1, "delete": 1}
ADMIN = {"role": "System Manager", "read": 1, "create": 1, "write": 1, "delete": 1}
READONLY_USER = {"role": "Investment User", "read": 1}
EVENT_USER = {"role": "Investment User", "read": 1, "create": 1, "write": 1, "submit": 1}


def main():
    defs = {
        "Investment Settings": [
            {"issingle": 1, "title": None},
            [
                link("company", "Company", reqd=1),
                select(
                    "default_cost_method", ["FIFO", "LIFO", "AVERAGE", "SPECIFIC"], default="FIFO", reqd=1
                ),
                select(
                    "auto_accounting",
                    ["Off", "Draft", "Submit"],
                    default="Draft",
                    reqd=1,
                    description="Whether submitted events create Journal Entries as drafts, submitted, or never.",
                ),
                select(
                    "price_provider",
                    ["Manual", "Stooq", "Broker", "Alpha Vantage", "CoinGecko (Pro)"],
                    default="Manual",
                ),
                field("price_sync_enabled", "Check", default="1"),
                field("broker_sync_enabled", "Check", default="1"),
                field("sync_interval_minutes", "Int", default="60"),
            ],
        ],
        "Investment License": [
            {"issingle": 1, "title": None},
            [
                field("license_key", "Long Text"),
                field("tier", read_only=1),
                field("status", read_only=1),
                field("customer", read_only=1),
                field("expires", "Date", read_only=1),
                field("validated_at", "Datetime", read_only=1),
                field("status_note", "Small Text", read_only=1),
            ],
        ],
        "Exchange": [
            {"title": "exchange_name"},
            [
                field("exchange_code", reqd=1, unique=1, description="MIC-style code, e.g. XNAS, XNSE, NSE"),
                field("exchange_name", reqd=1),
                link("currency", "Currency", reqd=1),
                field("timezone", default="UTC"),
            ],
        ],
        "Security": [
            {"title": "security_name"},
            [
                field("security_name", reqd=1, in_list_view=1),
                select("asset_class", ["Stock", "ETF", "Bond", "Fund", "Crypto"], reqd=1, in_list_view=1),
                link("currency", "Currency", reqd=1),
                field("ticker", in_list_view=1),
                link("exchange", "Exchange"),
                field("isin"),
                field("figi"),
                section("provider_symbols"),
                field("stooq_symbol"),
                field("alphavantage_symbol"),
                field("coingecko_id"),
                section("bond_details"),
                field("face_value", "Currency"),
                field("coupon_rate", "Percent"),
                select("coupon_frequency", ["1", "2", "4", "12"]),
                field("issue_date", "Date"),
                field("maturity_date", "Date"),
                select("day_count", ["30/360", "ACT/ACT", "ACT/360"]),
                select("status", ["Active", "Delisted"], default="Active"),
            ],
        ],
        "Portfolio": [
            {"title": "portfolio_name"},
            [
                field("portfolio_name", reqd=1, in_list_view=1),
                link("company", "Company", reqd=1),
                link("base_currency", "Currency"),
                select("cost_method", ["", "FIFO", "LIFO", "AVERAGE", "SPECIFIC"]),
                field("notes", "Small Text"),
            ],
        ],
        "Investment Account": [
            {"title": "account_name"},
            [
                field("account_name", reqd=1, in_list_view=1),
                link("portfolio", "Portfolio", reqd=1, in_list_view=1),
                link("broker_connection", "Broker Connection"),
                link("currency", "Currency", reqd=1),
                field("external_id"),
                link(
                    "broker_cash_account",
                    "Account",
                    description="ERPNext GL account holding this broker account's cash.",
                ),
                field("enabled", "Check", default="1"),
            ],
        ],
        "Broker Connection": [
            {"title": "connection_name"},
            [
                field("connection_name", reqd=1, in_list_view=1),
                select(
                    "broker",
                    ["Zerodha", "Alpaca", "Interactive Brokers", "CSV Import"],
                    reqd=1,
                    in_list_view=1,
                ),
                link("company", "Company", reqd=1),
                section("credentials"),
                field("api_key", "Password"),
                field("api_secret", "Password"),
                field("access_token", "Password"),
                field("flex_token", "Password"),
                field("flex_query_id"),
                field("sandbox", "Check", default="0"),
                section("sync"),
                field("enabled", "Check", default="0"),
                select(
                    "status",
                    ["Not Connected", "Connected", "Error", "Token Expired"],
                    default="Not Connected",
                    read_only=1,
                ),
                field("last_sync", "Datetime", read_only=1),
                field("last_error", "Small Text", read_only=1),
                field("sync_cursor", read_only=1),
            ],
        ],
        "Investment Event": [
            {"is_submittable": 1, "title": None},
            [
                select("event_type", EVENT_TYPES, reqd=1, in_list_view=1),
                field("posting_date", "Date", reqd=1, in_list_view=1),
                link("account", "Investment Account", reqd=1, in_list_view=1),
                link("security", "Security"),
                section("amounts"),
                field("qty", description="Decimal quantity; always positive."),
                field("price", description="Per-unit price in the event currency (clean price for bonds)."),
                field("amount"),
                field("gross"),
                field("fees"),
                field("taxes"),
                field("accrued_interest"),
                link("currency", "Currency", reqd=1),
                section("corporate_action"),
                field("split_ratio"),
                field("basis_allocation"),
                field("child_ratio"),
                link("child_security", "Security"),
                field("lot_ids", "Small Text"),
                section("fx"),
                link("target_currency", "Currency"),
                field("target_amount"),
                section("provenance"),
                select(
                    "source",
                    ["Manual", "Zerodha", "Alpaca", "Interactive Brokers", "CSV Import"],
                    default="Manual",
                    reqd=1,
                ),
                field("source_ref"),
                link("connection", "Broker Connection", read_only=1),
                field("dedupe_key", unique=1, hidden=1),
                link("reversal_of", "Investment Event", read_only=1),
                link("journal_entry", "Journal Entry", read_only=1),
                select(
                    "accounting_status",
                    ["Pending", "Posted", "Skipped", "Failed", "Not Applicable"],
                    default="Pending",
                    read_only=1,
                ),
                field("meta_json", "Long Text", hidden=1),
                field("notes", "Small Text"),
            ],
        ],
        "Tax Lot": [
            {"title": None},
            [
                link("account", "Investment Account", reqd=1),
                link("security", "Security", reqd=1),
                field("qty_open", reqd=1),
                field("qty_original", reqd=1),
                field("unit_cost", reqd=1),
                link("currency", "Currency", reqd=1),
                field("acquired", "Date", reqd=1),
                link("source_event", "Investment Event"),
                select("status", ["Open", "Closed", "Transferred"], default="Open"),
                field("engine_id", hidden=1),
            ],
        ],
        "Lot Allocation": [
            {"title": None},
            [
                link("event", "Investment Event", reqd=1),
                link("lot", "Tax Lot", reqd=1),
                field("qty", reqd=1),
                field("cost", reqd=1),
                field("proceeds", reqd=1),
                field("realized_pnl", reqd=1),
                field("acquired", "Date"),
            ],
        ],
        "Security Price": [
            {"title": None},
            [
                link("security", "Security", reqd=1, in_list_view=1),
                field("date", "Date", reqd=1, in_list_view=1),
                field("close", reqd=1),
                link("currency", "Currency"),
                select(
                    "source", ["Manual", "Stooq", "Broker", "Alpha Vantage", "CoinGecko"], default="Manual"
                ),
            ],
        ],
        "FX Rate": [
            {"title": None},
            [
                link("from_currency", "Currency", reqd=1),
                link("to_currency", "Currency", reqd=1),
                field("date", "Date", reqd=1),
                field("rate", reqd=1),
                field("source", default="Manual"),
            ],
        ],
        "Portfolio Snapshot": [
            {"title": None},
            [
                link("portfolio", "Portfolio", reqd=1, in_list_view=1),
                field("date", "Date", reqd=1, in_list_view=1),
                link("base_currency", "Currency"),
                field("total_value"),
                field("total_cost"),
                field("unrealized_pnl"),
                field("realized_pnl_period"),
                field("income_period"),
                field("external_flow"),
                field("twr_period"),
                field("xirr_period"),
                field("holdings", "Table", options="Portfolio Holding"),
            ],
        ],
        "Portfolio Holding": [
            {"istable": 1, "title": None},
            [
                link("security", "Security", reqd=1),
                field("qty"),
                field("price"),
                field("value"),
                field("cost"),
                field("unrealized_pnl"),
                link("currency", "Currency"),
            ],
        ],
        "Investment Accounting Policy": [
            {"title": "company"},
            [
                link("company", "Company", reqd=1, unique=1),
                select("mode", ["Off", "Draft", "Submit"], default="Draft"),
                field("mappings", "Table", options="Policy Mapping"),
            ],
        ],
        "Policy Mapping": [
            {"istable": 1, "title": None},
            [
                select("event_type", EVENT_TYPES, reqd=1, in_list_view=1),
                link("debit_account", "Account", reqd=1),
                link("credit_account", "Account", reqd=1),
                link("tax_debit_account", "Account"),
                link("fee_debit_account", "Account"),
                link("pnl_account", "Account"),
            ],
        ],
        "Broker Sync Log": [
            {"title": None},
            [
                link("connection", "Broker Connection", reqd=1),
                field("started_at", "Datetime"),
                field("finished_at", "Datetime"),
                select("status", ["Success", "Failed", "Partial"]),
                field("events_created", "Int", default="0"),
                field("positions_seen", "Int", default="0"),
                field("prices_seen", "Int", default="0"),
                field("error", "Long Text"),
            ],
        ],
        "Import Batch": [
            {"title": None},
            [
                select("source", ["CSV Import", "Interactive Brokers"], reqd=1),
                field("import_file", "Attach"),
                field("account", "Link", options="Investment Account", reqd=1),
                select("status", ["Draft", "Dry Run", "Imported", "Failed"], default="Draft"),
                field("total_rows", "Int", default="0"),
                field("imported", "Int", default="0"),
                field("failed", "Int", default="0"),
                field("errors", "Table", options="Import Error"),
            ],
        ],
        "Import Error": [
            {"istable": 1, "title": None},
            [
                field("row_no", "Int"),
                field("message", "Small Text"),
            ],
        ],
    }
    module = ROOT / "frappe_investing"
    for directory in (
        module,
        module / "doctype",
        module / "page",
        module / "page/investing",
        module / "page/broker_setup",
    ):
        write(directory / "__init__.py", "")
    for name, (extra, fields) in defs.items():
        slug = name.lower().replace(" ", "_")
        is_single = extra.pop("issingle", 0)
        is_child = extra.pop("istable", 0)
        title = extra.pop("title", None)
        if is_child:
            perms = []
        elif is_single:
            perms = [ADMIN, {"role": "Investment Manager", "read": 1}, {"role": "Investment User", "read": 1}]
        elif name == "Investment Event":
            perms = [EVENT_USER, MANAGER | {"cancel": 1}, ADMIN | {"submit": 1, "cancel": 1}]
        elif name in {
            "Tax Lot",
            "Lot Allocation",
            "Portfolio Snapshot",
            "Security Price",
            "FX Rate",
            "Broker Sync Log",
        }:
            perms = [READONLY_USER, MANAGER, ADMIN]
        else:
            perms = [USER, MANAGER, ADMIN]
        definition = {
            "doctype": "DocType",
            "name": name,
            "module": MODULE,
            "autoname": "hash",
            "engine": "InnoDB",
            "issingle": is_single,
            "istable": is_child,
            "is_submittable": extra.get("is_submittable", 0),
            "fields": fields,
            "field_order": [f["fieldname"] for f in fields],
            "permissions": perms,
            "track_changes": 0,
            "sort_field": "creation",
            "sort_order": "DESC",
        }
        if title:
            definition["title_field"] = title
        folder = module / "doctype" / slug
        write(folder / "__init__.py", "")
        write(folder / f"{slug}.json", json.dumps(definition, indent=2) + "\n")
        cls = name.replace(" ", "")
        write(
            folder / f"{slug}.py",
            f"from frappe_investing.documents import ManagedDocument\n\n\nclass {cls}(ManagedDocument):\n    pass\n",
        )
    write(ROOT / "modules.txt", f"{MODULE}\n")
    write(ROOT / "patches.txt", "[pre_model_sync]\n\n[post_model_sync]\n")
    for page, title in (("investing", "Investing"), ("broker_setup", "Broker Setup")):
        write(
            module / "page" / page / f"{page}.json",
            json.dumps(
                {
                    "doctype": "Page",
                    "name": page,
                    "page_name": page,
                    "title": title,
                    "module": MODULE,
                    "standard": "Yes",
                    "system_page": 0,
                    "roles": [
                        {"role": r} for r in ("Investment User", "Investment Manager", "System Manager")
                    ],
                },
                indent=2,
            )
            + "\n",
        )


if __name__ == "__main__":
    main()
