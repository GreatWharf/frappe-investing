"""Fixed-income math: coupon schedules, accrued interest, clean/dirty prices, YTM."""

from datetime import date, timedelta

from .money import dec

_DAYS = {"30/360", "ACT/ACT", "ACT/360"}


def coupon_schedule(issue, maturity, frequency=2):
    """Coupon payment dates strictly after issue, through maturity."""
    if frequency not in (1, 2, 4, 12):
        raise ValueError("Coupon frequency must be 1, 2, 4 or 12 per year.")
    step_months = 12 // frequency
    dates, cursor = [], maturity
    while cursor > issue:
        dates.append(cursor)
        month = cursor.month - step_months
        year = cursor.year
        while month <= 0:
            month += 12
            year -= 1
        try:
            cursor = date(year, month, cursor.day)
        except ValueError:  # e.g. Aug 31 → Feb 28/29
            cursor = date(year, month + 1, 1) - timedelta(days=1)
    return sorted(dates)


def _day_count(start, end, basis, frequency):
    if basis == "30/360":
        d1, d2 = min(start.day, 30), min(end.day, 30)
        return (
            dec(360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1))
            / dec(360 // frequency)
            * dec(1)
        )
    if basis == "ACT/360":
        return dec((end - start).days) / dec(360)
    return dec((end - start).days) / dec(365)  # ACT/ACT (ICMA single-period approximation)


def accrued_interest(*, face, coupon_rate, last_coupon, settle, frequency=2, basis="30/360"):
    if basis not in _DAYS:
        raise ValueError(f"Unsupported day-count basis: {basis}")
    if settle <= last_coupon:
        raise ValueError("Settlement must be after the last coupon date.")
    face, rate = dec(face), dec(coupon_rate)
    period_fraction = _day_count(last_coupon, settle, basis, frequency)
    annual = face * rate / dec(frequency)
    if basis == "30/360":
        fraction = period_fraction  # already normalized to the coupon period
    else:
        period_days = dec(365) / dec(frequency)
        fraction = dec((settle - last_coupon).days) / period_days
    return annual * fraction


def clean_price(dirty_price, accrued):
    return dec(dirty_price) - dec(accrued)


def ytm(*, settle, maturity, coupon_rate, frequency, dirty_price, face=100, guess=None):
    """Solve yield-to-maturity by bisection. Price and face in percent of par."""
    price, face, rate = dec(dirty_price), dec(face), dec(coupon_rate)
    schedule = [d for d in coupon_schedule(settle, maturity, frequency) if d > settle]
    if not schedule:
        raise ValueError("Bond must have remaining cash flows.")
    coupon = face * rate / dec(frequency)

    def npv(y):
        total = dec(0)
        for d in schedule:
            years = dec((d - settle).days) / dec(365)
            cf = coupon + (face if d == schedule[-1] else dec(0))
            total += cf / (1 + y) ** years
        return total

    lo, hi = dec("-0.95"), dec(10)
    if npv(lo) < price:
        raise ValueError("Yield solve failed: price too high for lower bound.")
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > price:
            lo = mid
        else:
            hi = mid
        if hi - lo < dec("0.0000001"):
            break
    return (lo + hi) / 2
