from datetime import date
from decimal import Decimal as D

import pytest

from frappe_investing.core import bonds, fx, income


def test_dividend_net_and_consistency_check():
    event = income.dividend(
        security="AAPL",
        shares=D(1000),
        dps=D("0.26"),
        wht_rate=D("0.15"),
        pay_date=date(2026, 2, 1),
        currency="USD",
    )
    assert event.gross == D("260.00")
    assert event.taxes == D("39.00")
    assert event.cash_flow() == D("221.00")


def test_dividend_rejects_negative_or_overtaxed_amounts():
    with pytest.raises(ValueError):
        income.dividend(
            security="AAPL",
            shares=D(10),
            dps=D("1"),
            wht_rate=D("1.5"),
            pay_date=date(2026, 2, 1),
            currency="USD",
        )


def test_coupon_schedule_semiannual():
    schedule = bonds.coupon_schedule(date(2024, 3, 15), date(2034, 3, 15), frequency=2)
    assert schedule[0] == date(2024, 9, 15)
    assert schedule[-1] == date(2034, 3, 15)
    assert len(schedule) == 20  # semiannual over 10 years; issue date is not a coupon


def test_accrued_interest_30_360_us():
    # 10,000 face, 6% annual, semiannual, settled 90 days into the 180-day period.
    accrued = bonds.accrued_interest(
        face=D(10000),
        coupon_rate=D("0.06"),
        last_coupon=date(2026, 1, 1),
        settle=date(2026, 4, 1),
        frequency=2,
        basis="30/360",
    )
    assert accrued == D("150.00")


def test_accrued_interest_actual_actual_icma():
    # 10,000 face, 5% annual, annual coupons 1 Jan; settle 1 Jul (181/365 of year).
    accrued = bonds.accrued_interest(
        face=D(10000),
        coupon_rate=D("0.05"),
        last_coupon=date(2026, 1, 1),
        settle=date(2026, 7, 1),
        frequency=1,
        basis="ACT/ACT",
    )
    expected = D(500) * D(181) / D(365)
    assert abs(accrued - expected) < D("0.000001")


def test_ytm_solver_recovers_known_yield():
    # 2-year bond, 5% annual coupons, priced at par → YTM must be 5%.
    y = bonds.ytm(
        settle=date(2026, 1, 1),
        maturity=date(2028, 1, 1),
        coupon_rate=D("0.05"),
        frequency=1,
        dirty_price=D(100),
        face=D(100),
    )
    assert abs(y - D("0.05")) < D("0.0001")
    # Priced below par → yield above coupon.
    y2 = bonds.ytm(
        settle=date(2026, 1, 1),
        maturity=date(2028, 1, 1),
        coupon_rate=D("0.05"),
        frequency=1,
        dirty_price=D("95"),
        face=D(100),
    )
    assert y2 > D("0.05")


def test_fx_convert_direct_inverted_and_cross():
    book = fx.RateBook()
    book.set("USD", "GBP", date(2026, 1, 1), D("0.8"))
    assert book.convert(D(100), "USD", "GBP", date(2026, 1, 1)) == D(80)
    assert book.convert(D(80), "GBP", "USD", date(2026, 1, 1)) == D(100)


def test_fx_missing_rate_fails_loudly():
    book = fx.RateBook()
    with pytest.raises(fx.MissingRate):
        book.convert(D(1), "USD", "EUR", date(2026, 1, 1))


def test_fx_same_currency_is_identity():
    assert fx.RateBook().convert(D(7), "USD", "USD", date(2026, 1, 1)) == D(7)
