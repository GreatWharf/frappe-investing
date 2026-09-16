from datetime import date
from decimal import Decimal

import pytest

from frappe_investing.core import events, money


def test_dec_accepts_decimal_int_str_and_rejects_float():
    assert money.dec("1.10") == Decimal("1.10")
    assert money.dec(3) == Decimal("3")
    with pytest.raises(TypeError):
        money.dec(1.1)  # floats are banned: 0.1 + 0.2 class of bugs


def test_quantize_uses_currency_precision_half_up():
    assert money.q(Decimal("10.005"), 2) == Decimal("10.01")
    assert money.q(Decimal("10.004"), 2) == Decimal("10.00")
    assert money.q(Decimal("0.123456789"), 8) == Decimal("0.12345679")


def test_buy_requires_security_qty_price_and_validates_positive():
    with pytest.raises(ValueError, match="security"):
        events.Event(
            type="Buy", date=date(2026, 1, 1), account="a", currency="USD", qty=Decimal(10), price=Decimal(5)
        ).validate()
    with pytest.raises(ValueError, match="positive"):
        events.Event(
            type="Buy",
            date=date(2026, 1, 1),
            account="a",
            currency="USD",
            security="S",
            qty=Decimal(0),
            price=Decimal(5),
        ).validate()


def test_split_requires_ratio_and_dividend_requires_gross():
    with pytest.raises(ValueError, match="ratio"):
        events.Event(
            type="Split", date=date(2026, 1, 1), account="a", currency="USD", security="S"
        ).validate()
    with pytest.raises(ValueError, match="gross"):
        events.Event(
            type="Dividend", date=date(2026, 1, 1), account="a", currency="USD", security="S"
        ).validate()


def test_cash_flow_sign_convention():
    base = dict(date=date(2026, 1, 1), account="a", currency="USD")
    buy = events.Event(
        type="Buy", qty=Decimal(10), price=Decimal("20.5"), fees=Decimal("5"), security="S", **base
    )
    assert buy.cash_flow() == Decimal("-210")  # 205 + 5 fee
    sell = events.Event(
        type="Sell",
        qty=Decimal(10),
        price=Decimal("25"),
        fees=Decimal("4"),
        taxes=Decimal("1"),
        security="S",
        **base,
    )
    assert sell.cash_flow() == Decimal("245")
    div = events.Event(type="Dividend", gross=Decimal("100"), taxes=Decimal("15"), security="S", **base)
    assert div.cash_flow() == Decimal("85")
    dep = events.Event(type="Deposit", amount=Decimal("1000"), **base)
    assert dep.cash_flow() == Decimal("1000")
    wd = events.Event(type="Withdrawal", amount=Decimal("1000"), **base)
    assert wd.cash_flow() == Decimal("-1000")


def test_spin_off_requires_child_and_allocation():
    with pytest.raises(ValueError, match="child"):
        events.Event(
            type="Spin-off",
            date=date(2026, 1, 1),
            account="a",
            currency="USD",
            security="P",
            basis_allocation=Decimal("0.2"),
        ).validate()
    with pytest.raises(ValueError, match="allocation"):
        events.Event(
            type="Spin-off",
            date=date(2026, 1, 1),
            account="a",
            currency="USD",
            security="P",
            child_security="C",
        ).validate()
