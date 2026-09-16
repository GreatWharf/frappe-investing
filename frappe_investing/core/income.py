"""Income event construction. Broker-confirmed amounts always win over computed ones."""

from .events import COUPON, DIVIDEND, INTEREST, Event
from .money import dec, q


def dividend(
    *,
    security,
    shares,
    dps,
    wht_rate=0,
    pay_date,
    currency,
    account="",
    source="Manual",
    source_ref="",
    fees=0,
    reinvests=False,
):
    """Build a dividend event from declared per-share terms."""
    shares, dps, wht_rate = dec(shares), dec(dps), dec(wht_rate)
    if not 0 <= wht_rate < 1:
        raise ValueError("Withholding rate must be between 0 and 1.")
    gross = q(shares * dps, 2)
    taxes = q(gross * wht_rate, 2)
    if taxes > gross:
        raise ValueError("Withholding cannot exceed the gross dividend.")
    return Event(
        type=DIVIDEND,
        date=pay_date,
        account=account,
        currency=currency,
        security=security,
        qty=shares,
        gross=gross,
        taxes=taxes,
        fees=dec(fees),
        source=source,
        source_ref=source_ref,
        meta={"reinvests": bool(reinvests)},
    )


def coupon(
    *,
    security,
    face,
    coupon_rate,
    period_days,
    year_days,
    pay_date,
    currency,
    account="",
    wht_rate=0,
    source="Manual",
    source_ref="",
):
    gross = q(dec(face) * dec(coupon_rate) * dec(period_days) / dec(year_days), 2)
    taxes = q(gross * dec(wht_rate), 2)
    return Event(
        type=COUPON,
        date=pay_date,
        account=account,
        currency=currency,
        security=security,
        qty=dec(face),
        gross=gross,
        taxes=taxes,
        source=source,
        source_ref=source_ref,
    )


def interest(*, amount, pay_date, currency, account="", taxes=0, source="Manual", source_ref=""):
    gross = q(dec(amount), 2)
    return Event(
        type=INTEREST,
        date=pay_date,
        account=account,
        currency=currency,
        gross=gross,
        taxes=dec(taxes),
        source=source,
        source_ref=source_ref,
    )
