"""Doctype permission tables on disk: role split for events, connections, policy, license.

6. Investment Event: submit belongs to Manager (/System Manager); Users
   record only (read/create/write, no submit/cancel). Cancel stays Manager.
7. Broker Connection + Investment Accounting Policy: Users read-only;
   create/write/delete Manager-only (User write on secrets and GL mappings
   would leak credentials and let anyone rewrite accounting rules).
8. Investment License: license_key is a Password at permlevel 1, granted to
   managers only; cloud_plan is read-only so Desk cannot rewrite the cached
   plan inside the grace window.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "frappe_investing" / "investing" / "doctype"


def load(doctype):
    with open(ROOT / doctype / f"{doctype}.json") as handle:
        return json.load(handle)


def perms(doctype):
    return {entry["role"]: entry for entry in load(doctype)["permissions"]}


def fields(doctype):
    return {entry["fieldname"]: entry for entry in load(doctype)["fields"]}


# ------------------------------------------------------------------- (6) event
def test_users_record_events_but_cannot_submit():
    user = perms("investment_event")["Investment User"]
    assert user.get("create") == 1 and user.get("write") == 1
    assert not user.get("submit") and not user.get("cancel")


def test_managers_submit_and_cancel_events():
    manager = perms("investment_event")["Investment Manager"]
    assert manager.get("submit") == 1 and manager.get("cancel") == 1


# --------------------------------------- (7) connection secrets + GL mappings
def test_users_cannot_write_broker_connections():
    user = perms("broker_connection")["Investment User"]
    assert user.get("read") == 1
    assert not user.get("create") and not user.get("write")


def test_users_cannot_write_accounting_policy():
    user = perms("investment_accounting_policy")["Investment User"]
    assert user.get("read") == 1
    assert not user.get("create") and not user.get("write")


# ------------------------------------------------------- (8) license hardening
def test_license_key_is_restricted_password():
    field = fields("investment_license")["license_key"]
    assert field["fieldtype"] == "Password"
    assert field.get("permlevel") == 1
    table = perms("investment_license")
    assert table["Investment User"].get("permlevel", 0) != 1 or not table["Investment User"].get("read")
    manager_rows = [e for e in load("investment_license")["permissions"] if e["role"] == "Investment Manager"]
    assert any(e.get("permlevel") == 1 for e in manager_rows)


def test_cloud_plan_is_read_only():
    assert fields("investment_license")["cloud_plan"].get("read_only") == 1
