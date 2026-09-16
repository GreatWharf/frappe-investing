"""Generic CSV import tests: parsing, validation, deterministic source refs."""

import hashlib
import json
from decimal import Decimal

from frappe_investing.connectors import csv_import
from tests.fixtures.fakes import load_fixture


def test_parse_good_csv_yields_full_normalized_events():
    result = csv_import.parse_csv(load_fixture("import_good.csv"), account="Zerodha Main")
    assert result["errors"] == []
    events = result["events"]
    assert len(events) == 5

    buy = events[0]
    assert buy["type"] == "Buy"
    assert buy["security_key"] == "US:AAPL"
    assert buy["qty"] == "10"
    assert buy["price"] == "190.25"
    assert buy["amount"] is None
    assert buy["gross"] is None
    assert buy["fees"] == "1.00"
    assert buy["taxes"] == "0"
    assert buy["currency"] == "USD"
    assert buy["date"] == "2026-01-15"
    assert buy["source_ref"] == "ORD-1"
    assert buy["meta"]["csv_row"] == 2
    assert buy["notes"] == "first buy"
    assert Decimal(buy["qty"]) == 10 and Decimal(buy["price"]) == Decimal("190.25")

    dividend = events[1]
    assert dividend["type"] == "Dividend"
    assert dividend["gross"] == "24.50"
    assert dividend["taxes"] == "3.68"

    split = events[2]
    assert split["type"] == "Split"
    assert split["split_ratio"] == "3"
    assert Decimal(split["split_ratio"]) == 3

    deposit = events[3]
    assert deposit["type"] == "Deposit"
    assert deposit["amount"] == "5000"
    assert deposit["security_key"] is None

    spinoff = events[4]
    assert spinoff["type"] == "Spin-off"
    assert spinoff["security_key"] == "US:ABC"
    assert spinoff["basis_allocation"] == "0.25"
    assert spinoff["child_security"] == "US:XYZ"
    assert spinoff["child_ratio"] == "0.5"


def test_missing_source_ref_is_deterministic_content_hash():
    expected_row = {
        "date": "2026-03-05",
        "type": "Deposit",
        "security_key": "",
        "qty": "",
        "price": "",
        "amount": "5000",
        "gross": "",
        "fees": "",
        "taxes": "0",
        "currency": "USD",
        "split_ratio": "",
        "basis_allocation": "",
        "child_security": "",
        "child_ratio": "",
        "notes": "initial funding",
        "source_ref": "",
    }
    canonical = json.dumps(expected_row, sort_keys=True, separators=(",", ":"))
    expected_ref = hashlib.sha256(f"csv:{canonical}".encode("utf-8")).hexdigest()[:32]

    first = csv_import.parse_csv(load_fixture("import_good.csv"))
    second = csv_import.parse_csv(load_fixture("import_good.csv"))
    deposit = first["events"][3]
    assert deposit["source_ref"] == expected_ref
    assert second["events"][3]["source_ref"] == expected_ref  # re-import is idempotent
    assert len(deposit["source_ref"]) == 32
    int(deposit["source_ref"], 16)  # hex


def test_bad_rows_are_reported_with_line_numbers_and_good_rows_still_returned():
    result = csv_import.parse_csv(load_fixture("import_bad.csv"), account="Zerodha Main")
    # caller gets the full picture and decides: never a partial silent commit
    assert len(result["events"]) == 1
    assert result["events"][0]["source_ref"] == "BAD-OK"

    errors = {e["row"]: e["message"] for e in result["errors"]}
    assert sorted(errors) == [3, 4, 5, 6, 7]
    assert "date" in errors[3].lower() and "2026-13-40" in errors[3]
    assert "positive" in errors[4]  # core Event.validate rejects qty <= 0
    assert "decimal" in errors[5].lower() and "not-a-number" in errors[5]
    assert "Bogus" in errors[6]
    assert "gross" in errors[7]


def test_validation_runs_through_core_event_validate():
    text = (
        "date,type,security_key,gross,currency,source_ref\n"
        "2026-02-01,Dividend,,24.50,USD,X1\n"
    )  # dividend without a security
    result = csv_import.parse_csv(text)
    assert result["events"] == []
    assert "security" in result["errors"][0]["message"]


def test_column_mapping_renames_broker_headers():
    text = "Trade Date,Action,Ticker,Shares,Cost,CCY\n2026-01-15,Buy,US:AAPL,10,190.25,USD\n"
    mapping = {
        "Trade Date": "date",
        "Action": "type",
        "Ticker": "security_key",
        "Shares": "qty",
        "Cost": "price",
        "CCY": "currency",
    }
    result = csv_import.parse_csv(text, mapping=mapping)
    assert result["errors"] == []
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["type"] == "Buy"
    assert event["security_key"] == "US:AAPL"
    assert event["qty"] == "10"
    assert event["price"] == "190.25"
    assert event["currency"] == "USD"


def test_empty_csv_reports_error():
    result = csv_import.parse_csv("")
    assert result["events"] == []
    assert result["errors"]


def test_blank_and_whitespace_rows_are_skipped():
    text = (
        "date,type,security_key,qty,price,currency,source_ref\n"
        "\n"
        "2026-01-15,Buy,US:AAPL,10,190.25,USD,R1\n"
        ",,,,,,,\n"
    )
    result = csv_import.parse_csv(text)
    assert len(result["events"]) == 1
    assert result["errors"] == []
