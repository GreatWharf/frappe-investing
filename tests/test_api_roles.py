"""API role-gate tests: _manager/_user enforcement with only_for honored.

The shared harness's only_for enforces Investment roles by default (raising
PermissionError when the caller's roles lack them), so these tests prove the
gates are wired — not stubbed to no-op. Manager-only methods reject a
plain Investment User; user-level methods accept them.
"""

import pytest

from tests.support import evict_app_modules, fake_frappe, seed, store  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    seed(store)
    fake = fake_frappe(monkeypatch)
    import frappe_investing.api as api_mod

    yield api_mod, fake, store
    evict_app_modules()

def as_roles(fake, roles):
    fake.current_roles["roles"] = list(roles)


def test_create_portfolio_rejects_plain_user(api):
    api_mod, fake, store = api
    as_roles(fake, ["Investment User"])
    store["Acme-Co"] = store["Acme"]
    with pytest.raises(PermissionError):
        api_mod.create_portfolio("X", "Acme")


def test_record_manual_event_accepts_plain_user(api, monkeypatch):
    api_mod, fake, store = api
    as_roles(fake, ["Investment User"])
    calls = []
    monkeypatch.setattr(
        api_mod.services, "record_event", lambda data: (calls.append(data) or ("EV-1", True))
    )
    result = api_mod.record_manual_event(
        account="a1", event_type="Buy", security="sec-aapl", qty="10", price="10", currency="USD"
    )
    # The plain user passes the _user() role gate and reaches record_event
    # with a forced Manual source; the stubbed return proves the wiring.
    assert calls and calls[0]["source"] == "Manual"
    assert result == {"name": "EV-1", "created": True}


def test_create_portfolio_rejects_users_with_no_role(api):
    api_mod, fake, store = api
    as_roles(fake, ["Customer"])
    with pytest.raises(PermissionError):
        api_mod.create_portfolio("X", "Acme")


def test_manager_methods_accept_investment_manager(api):
    api_mod, fake, store = api
    as_roles(fake, ["Investment Manager"])
    assert fake.only_for is not None
    # only_for honored: manager passes the gate, then fails later on missing doc.
    with pytest.raises(KeyError):
        api_mod.create_portfolio("X", "No-Such-Company")


def test_system_manager_passes_manager_gates(api):
    api_mod, fake, store = api
    as_roles(fake, ["System Manager"])
    with pytest.raises(KeyError):
        api_mod.create_portfolio("X", "No-Such-Company")


def test_license_status_accepts_plain_user(api, monkeypatch):
    api_mod, fake, store = api
    as_roles(fake, ["Investment User"])
    monkeypatch.setattr(api_mod.license_service, "public_state", lambda: {"tier": "standard"})
    assert api_mod.license_status()["tier"] == "standard"
