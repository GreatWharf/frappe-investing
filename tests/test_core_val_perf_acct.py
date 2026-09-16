from datetime import date
from decimal import Decimal as D

import pytest

from frappe_investing.core import accounting, performance, valuation
from frappe_investing.core.events import Event
from frappe_investing.core.fx import RateBook
from frappe_investing.core.lots import LotEngine


def setup_engine():
    eng = LotEngine()
    day = date(2026, 1, 5)
    eng.apply(
        Event(
            type="Buy", date=day, account="IB", currency="USD", security="AAPL", qty=D(100), price=D(10)
        ).validate()
    )
    eng.apply(
        Event(
            type="Buy",
            date=day,
            account="IB",
            currency="USD",
            security="VOD.L",
            qty=D(200),
            price=D("1"),
        ).validate()
    )
    return eng, day


def prices_for(day):
    return {("AAPL", day): D("11"), ("VOD.L", day): D("1.10")}


def test_valuation_computes_value_cost_and_unrealized_per_position():
    eng, day = setup_engine()
    book = RateBook()
    book.set("USD", "USD", day, 1)
    result = valuation.positions_value(
        eng, prices_for(day), day, base="USD", fx=book, asset_classes={"AAPL": "Equity", "VOD.L": "Equity"}
    )
    assert result["total_value"] == D("1320.00")  # 1100 + 220
    assert result["total_cost"] == D("1200.00")
    assert result["unrealized_pnl"] == D("120.00")
    assert result["by_security"]["AAPL"]["market_value"] == D("1100.00")


def test_valuation_translates_currencies_and_fails_on_missing_rate():
    eng, day = setup_engine()
    book = RateBook()
    book.set("USD", "USD", day, 1)
    ccy = {"AAPL": "USD", "VOD.L": "GBP"}
    with pytest.raises(Exception):
        valuation.positions_value(
            eng,
            prices_for(day),
            day,
            base="GBP",
            fx=book,
            asset_classes={"AAPL": "Equity", "VOD.L": "Equity"},
            currencies=ccy,
        )
    book.set("USD", "GBP", day, D("0.8"))
    result = valuation.positions_value(
        eng,
        prices_for(day),
        day,
        base="GBP",
        fx=book,
        asset_classes={"AAPL": "Equity", "VOD.L": "Equity"},
        currencies=ccy,
    )
    assert result["by_security"]["AAPL"]["market_value"] == D("880.00")  # $1100 × 0.8
    assert result["by_security"]["VOD.L"]["market_value"] == D("220.00")  # £220 unchanged
    assert result["total_value"] == D("1100.00")
    alloc = valuation.allocation(result["by_security"], "asset_class")
    assert alloc == {"Equity": D("100.00")}


def test_missing_price_is_flagged_not_silently_zero():
    eng, day = setup_engine()
    book = RateBook()
    book.set("USD", "USD", day, 1)
    result = valuation.positions_value(
        eng,
        {("AAPL", day): D("11")},
        day,
        base="USD",
        fx=book,
        asset_classes={"AAPL": "Equity", "VOD.L": "Equity"},
    )
    assert result["stale"] == ["VOD.L"]
    assert result["by_security"]["VOD.L"]["market_value"] is None


def test_twr_ignores_external_cash_flows():
    # Day 1: 1000 value. Day 2: deposit 500, market unchanged → return 0, not 50%.
    snaps = [
        performance.Snapshot(day=date(2026, 1, 1), value=D(1000), external_flow=D(0)),
        performance.Snapshot(day=date(2026, 1, 2), value=D(1500), external_flow=D(500)),
        performance.Snapshot(day=date(2026, 1, 3), value=D(1575), external_flow=D(0)),
    ]
    twr = performance.twr(snaps)
    assert abs(twr - D("0.05")) < D("0.000001")  # 5% on day 3 only


def test_xirr_single_investment_doubling_in_a_year():
    r = performance.xirr([(date(2025, 1, 1), D(-1000))], D(2000), date(2026, 1, 1))
    assert abs(r - D("1.0")) < D("0.001")


def test_xirr_with_interim_dividend():
    flows = [(date(2025, 1, 1), D(-1000)), (date(2025, 7, 1), D(50))]
    r = performance.xirr(flows, D(1100), date(2026, 1, 1))
    assert r > D("0.14") and r < D("0.16")


def test_xirr_rejects_unsigned_flows():
    with pytest.raises(ValueError):
        performance.xirr([(date(2025, 1, 1), D(100))], D(100), date(2026, 1, 1))


def test_policy_maps_dividend_to_three_line_journal():
    policy = accounting.Policy(
        company_currency="GBP",
        rules={
            "Dividend": accounting.Rule(
                debit="Broker Cash - GW",
                credit="Dividend Income - GW",
                tax_debit="Withholding Tax Receivable - GW",
            ),
        },
    )
    event = Event(
        type="Dividend",
        date=date(2026, 2, 1),
        account="IB",
        currency="USD",
        security="AAPL",
        gross=D("260"),
        taxes=D("39"),
    ).validate()
    book = RateBook()
    book.set("USD", "GBP", event.date, D("0.8"))
    lines = accounting.journal_lines(event, policy, rates=book, on=event.date)
    assert lines[0] == accounting.Line(account="Broker Cash - GW", debit=D("176.80"), credit=D(0))
    assert lines[1] == accounting.Line(
        account="Withholding Tax Receivable - GW", debit=D("31.20"), credit=D(0)
    )
    assert lines[2] == accounting.Line(account="Dividend Income - GW", debit=D(0), credit=D("208.00"))
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)


def test_policy_maps_buy_sell_and_fee_with_balanced_entries():
    policy = accounting.Policy(
        company_currency="USD",
        rules={
            "Buy": accounting.Rule(debit="Equity Investments", credit="Broker Cash"),
            "Sell": accounting.Rule(
                debit="Broker Cash", credit="Equity Investments", pnl_account="Realised Investment Gains"
            ),
            "Fee": accounting.Rule(debit="Brokerage Fees", credit="Broker Cash"),
        },
    )
    eng, day = setup_engine()
    buy = Event(
        type="Buy",
        date=day,
        account="IB",
        currency="USD",
        security="MSFT",
        qty=D(10),
        price=D(100),
        fees=D(5),
    ).validate()
    lines = accounting.journal_lines(buy, policy, rates=RateBook(), on=day)
    assert sum(line.debit for line in lines) == D(1005) == sum(line.credit for line in lines)
    eng.apply(buy)
    sell = Event(
        type="Sell", date=day, account="IB", currency="USD", security="MSFT", qty=D(10), price=D(120)
    ).validate()
    allocations = eng.apply(sell).allocations
    lines = accounting.journal_lines(sell, policy, rates=RateBook(), on=day, allocations=allocations)
    by_account = {line.account: line for line in lines}
    assert by_account["Broker Cash"].debit == D(1200)
    assert by_account["Equity Investments"].credit == D(1005)
    assert by_account["Realised Investment Gains"].credit == D(195)


def test_unmapped_event_type_fails_closed():
    policy = accounting.Policy(company_currency="USD", rules={})
    event = Event(
        type="Buy", date=date(2026, 1, 1), account="IB", currency="USD", security="MSFT", qty=D(1), price=D(1)
    ).validate()
    with pytest.raises(accounting.UnmappedEvent):
        accounting.journal_lines(event, policy, rates=RateBook(), on=event.date)
