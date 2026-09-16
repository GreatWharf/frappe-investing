"""Performance: daily-linked TWR and XIRR. External flows never count as return."""

from dataclasses import dataclass
from datetime import date

from .money import dec, q

TRADING_DAYS = 252


@dataclass
class Snapshot:
    day: date
    value: object  # end-of-day value in base currency
    external_flow: object  # net deposits (+) / withdrawals (−) that day, base currency


def twr(snapshots):
    """Chain-linked time-weighted return. Flows are assumed to land before valuation."""
    if len(snapshots) < 2:
        return dec(0)
    factor = dec(1)
    for prev, cur in zip(snapshots, snapshots[1:]):
        base = dec(prev.value) + dec(cur.external_flow)
        if base == 0:
            continue
        factor *= dec(cur.value) / base
    return factor - 1


def _npv(rate, flows, terminal, as_of):
    """All flows discounted to the first flow's date (actual/365, as in Excel XIRR)."""
    origin = flows[0][0]
    total = dec(0)
    for day, amount in flows:
        years = dec((day - origin).days) / dec(365)
        total += dec(amount) / (1 + rate) ** years
    terminal_years = dec((as_of - origin).days) / dec(365)
    return total + dec(terminal) / (1 + rate) ** terminal_years


def xirr(flows, terminal_value, as_of):
    """Money-weighted return. Flows: investments negative, returns to investor positive."""
    flows = [(day, dec(amount)) for day, amount in flows]
    if not any(amount < 0 for _, amount in flows) or not (
        any(amount > 0 for _, amount in flows) or dec(terminal_value) > 0
    ):
        raise ValueError("XIRR needs at least one outflow and one inflow/terminal value.")
    lo, hi = dec("-0.9999"), dec(100)
    if _npv(lo, flows, terminal_value, as_of) * _npv(hi, flows, terminal_value, as_of) > 0:
        raise ValueError("XIRR is not bracketed for these cash flows.")
    for _ in range(300):
        mid = (lo + hi) / 2
        if _npv(lo, flows, terminal_value, as_of) * _npv(mid, flows, terminal_value, as_of) <= 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < dec("0.000000001"):
            break
    return (lo + hi) / 2


def income_total(events, from_date=None, to_date=None):
    from .events import INCOME_EVENTS

    total = dec(0)
    for event in events:
        if event.type in INCOME_EVENTS and (
            from_date is None or from_date <= event.date <= (to_date or date.max)
        ):
            total += event.net_income()
    return total


def daily_returns(snapshots):
    """Per-day returns, with each day's external flow removed from the numerator.

    Returns a list of (day, return) for consecutive snapshot pairs where the
    invested base is non-zero. Decimal throughout.
    """
    returns = []
    for prev, cur in zip(snapshots, snapshots[1:]):
        base = dec(prev.value) + dec(cur.external_flow)
        if base == 0:
            continue
        returns.append((cur.day, (dec(cur.value) - base) / base))
    return returns


def sharpe_ratio(snapshots, *, risk_free_annual=0, periods_per_year=TRADING_DAYS):
    """Annualized Sharpe from per-day returns: mean(excess)/sigma × √periods.

    Returns None when fewer than two usable daily returns exist or when the
    return series has zero standard deviation (undefined, not 0 — a flat line
    has no meaningful Sharpe). risk_free_annual is a ratio (0.04 = 4%).
    """
    returns = [r for _day, r in daily_returns(snapshots)]
    if len(returns) < 2:
        return None
    n = dec(len(returns))
    mean = sum(returns, dec(0)) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)  # sample
    if variance == 0:
        return None
    sigma = variance.sqrt()
    rf_daily = dec(risk_free_annual) / dec(periods_per_year)
    sharpe = (mean - rf_daily) / sigma * dec(periods_per_year).sqrt()
    return q(sharpe, 4)
