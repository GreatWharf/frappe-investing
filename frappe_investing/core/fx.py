"""FX conversion with explicit missing-rate failure. Rates are date-stamped."""

from .money import dec


class MissingRate(LookupError):
    pass


class RateBook:
    def __init__(self):
        self._rates = {}  # (from, to, date) -> Decimal

    def set(self, from_ccy, to_ccy, day, rate):
        rate = dec(rate)
        if rate <= 0:
            raise ValueError("FX rates must be positive.")
        self._rates[(from_ccy, to_ccy, day)] = rate

    def rate(self, from_ccy, to_ccy, day):
        if from_ccy == to_ccy:
            return dec(1)
        direct = self._rates.get((from_ccy, to_ccy, day))
        if direct is not None:
            return direct
        inverted = self._rates.get((to_ccy, from_ccy, day))
        if inverted is not None:
            return dec(1) / inverted
        raise MissingRate(f"No FX rate {from_ccy}/{to_ccy} on {day}.")

    def convert(self, amount, from_ccy, to_ccy, day):
        return dec(amount) * self.rate(from_ccy, to_ccy, day)
