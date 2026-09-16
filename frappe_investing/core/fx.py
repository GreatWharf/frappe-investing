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

    def rate_on_or_before(self, from_ccy, to_ccy, day):
        """Latest rate at or before `day` — for point-in-time displays like
        benchmark comparisons, where quoting-day gaps are normal."""
        if from_ccy == to_ccy:
            return dec(1)
        for a, b, invert in ((from_ccy, to_ccy, False), (to_ccy, from_ccy, True)):
            dated = [(d, r) for (f, t, d), r in self._rates.items() if (f, t) == (a, b) and d <= day]
            if dated:
                rate = max(dated)[1]
                return dec(1) / rate if invert else rate
        raise MissingRate(f"No FX rate {from_ccy}/{to_ccy} on or before {day}.")

    def convert(self, amount, from_ccy, to_ccy, day):
        return dec(amount) * self.rate(from_ccy, to_ccy, day)

    def convert_on_or_before(self, amount, from_ccy, to_ccy, day):
        return dec(amount) * self.rate_on_or_before(from_ccy, to_ccy, day)
