"""Decimal money helpers. Floats are banned at the boundary — coerce explicitly."""

from decimal import ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 28


def dec(value):
    """Coerce to Decimal. Floats are rejected so binary error never enters the ledger."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("Floats are not allowed in investment accounting; pass str/int/Decimal.")
    return Decimal(value)


def q(value, precision=2):
    return dec(value).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def is_zero(value):
    return dec(value) == 0
