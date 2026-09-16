from datetime import date
from decimal import Decimal as D

import pytest

from frappe_investing.core.events import Event
from frappe_investing.core.lots import LotEngine, LotError


def ev(type, **kw):
    base = dict(date=date(2026, 1, 10), account="IB", currency="USD", security="AAPL")
    return Event(type=type, **{**base, **kw}).validate()


def buy(qty, price, fees="0", **kw):
    return ev("Buy", qty=D(qty), price=D(price), fees=D(fees), **kw)


def sell(qty, price, fees="0", **kw):
    return ev("Sell", qty=D(qty), price=D(price), fees=D(fees), **kw)


def test_buy_creates_lot_with_fees_in_cost_basis():
    eng = LotEngine()
    eng.apply(buy(100, 10, fees="5"))
    (lot,) = eng.open_lots("IB", "AAPL")
    assert lot.qty == D(100)
    assert lot.unit_cost == D("10.05")  # (1000+5)/100
    assert eng.position("IB", "AAPL") == D(100)


def test_fifo_sell_allocates_oldest_lots_and_computes_realized_pnl():
    eng = LotEngine()
    eng.apply(buy(50, 10))  # cost 500
    eng.apply(buy(50, 12))  # cost 600
    result = eng.apply(sell(60, 15))  # proceeds 900
    assert len(result.allocations) == 2
    first, second = result.allocations
    assert first.qty == D(50) and first.realized_pnl == D(250)  # 750-500
    assert second.qty == D(10) and second.realized_pnl == D(30)  # 150-120
    assert result.total_realized_pnl() == D(280)
    assert eng.position("IB", "AAPL") == D(40)
    assert eng.open_lots("IB", "AAPL")[0].unit_cost == D(12)


def test_lifo_uses_newest_first():
    eng = LotEngine(method="LIFO")
    eng.apply(buy(10, 10))
    eng.apply(buy(10, 20))
    result = eng.apply(sell(5, 30))
    assert result.allocations[0].realized_pnl == D(50)  # 150 - 100 (from the 20-cost lot)


def test_average_cost_pools_basis():
    eng = LotEngine(method="AVERAGE")
    eng.apply(buy(10, 10))
    eng.apply(buy(10, 20))  # pool 20 @ avg 15
    result = eng.apply(sell(5, 30))
    assert result.allocations[0].cost == D(75)
    assert result.total_realized_pnl() == D(75)
    (pool,) = eng.open_lots("IB", "AAPL")
    assert pool.qty == D(15) and pool.unit_cost == D(15)


def test_specific_id_requires_lot_and_uses_it():
    eng = LotEngine(method="SPECIFIC")
    eng.apply(buy(10, 10))
    eng.apply(buy(10, 20))
    target = eng.open_lots("IB", "AAPL")[1]
    with pytest.raises(LotError):
        eng.apply(sell(1, 30))  # no lot chosen
    result = eng.apply(sell(1, 30, lot_ids=(target.id,)))
    assert result.allocations[0].lot_id == target.id
    assert result.allocations[0].realized_pnl == D(10)


def test_oversell_is_rejected():
    eng = LotEngine()
    eng.apply(buy(10, 10))
    with pytest.raises(LotError, match="Insufficient"):
        eng.apply(sell(11, 12))


def test_four_for_one_split_preserves_total_cost_basis():
    eng = LotEngine()
    eng.apply(buy(25, 400))  # £10,000 basis
    eng.apply(ev("Split", split_ratio=D(4)))
    (lot,) = eng.open_lots("IB", "AAPL")
    assert lot.qty == D(100)
    assert lot.unit_cost == D(100)
    assert lot.qty * lot.unit_cost == D(10000)


def test_reverse_split_and_cash_in_lieu_of_fractional_share():
    eng = LotEngine()
    eng.apply(buy(3, 100))
    eng.apply(ev("Reverse Split", split_ratio=D("0.5")))  # 1.5 shares @ 200
    (lot,) = eng.open_lots("IB", "AAPL")
    assert lot.qty == D("1.5") and lot.unit_cost == D(200)
    result = eng.apply(ev("Cash-in-lieu", qty=D("0.5"), price=D("190")))
    assert result.total_realized_pnl() == D("95") - D(100)
    assert eng.position("IB", "AAPL") == D(1)


def test_stock_dividend_dilutes_basis_by_default():
    eng = LotEngine()
    eng.apply(buy(100, 50))  # 5000
    eng.apply(ev("Stock Dividend", split_ratio=D("0.1")))  # +10 shares
    (lot,) = eng.open_lots("IB", "AAPL")
    assert lot.qty == D(110)
    assert lot.qty * lot.unit_cost == D(5000)


def test_spin_off_allocates_basis_and_creates_child_lots():
    eng = LotEngine()
    eng.apply(buy(100, 40))  # parent basis 4000
    result = eng.apply(ev("Spin-off", child_security="XYZ", basis_allocation=D("0.25"), child_ratio=D("0.5")))
    (parent,) = eng.open_lots("IB", "AAPL")
    assert parent.qty == D(100)
    assert parent.qty * parent.unit_cost == D(3000)
    (child,) = eng.open_lots("IB", "XYZ")
    assert child.qty == D(50)
    assert child.qty * child.unit_cost == D(1000)
    assert result.total_cost() == D(4000)


def test_transfer_out_then_in_preserves_cost_and_date():
    eng = LotEngine()
    eng.apply(buy(10, 10))
    eng.apply(ev("Transfer Out", qty=D(10), price=D(0)))
    assert eng.position("IB", "AAPL") == 0
    moved = eng.transferred[("IB", "AAPL")]
    eng.apply(ev("Transfer In", qty=D(10), account="SAXO", price=D(0), meta={"lots": moved}))
    (lot,) = eng.open_lots("SAXO", "AAPL")
    assert lot.unit_cost == D(10) and lot.acquired == date(2026, 1, 10)


def test_redemption_closes_bond_position_at_redemption_price():
    eng = LotEngine()
    eng.apply(ev("Buy", security="UK1T", qty=D(10000), price=D("0.98")))  # £9,800 for £10k nominal
    result = eng.apply(ev("Redemption", security="UK1T", qty=D(10000), price=D(1)))
    assert result.total_realized_pnl() == D(200)
    assert eng.position("IB", "UK1T") == 0
