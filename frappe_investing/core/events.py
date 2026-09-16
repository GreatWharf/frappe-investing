"""Normalized investment events: the single ingest format for brokers, CSV and manual entry.

Sign convention: cash_flow() returns the effect on the account's cash in `currency`.
Quantities are always positive; the event type carries direction.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .money import dec

BUY = "Buy"
SELL = "Sell"
DIVIDEND = "Dividend"
COUPON = "Coupon"
INTEREST = "Interest"
FEE = "Fee"
DEPOSIT = "Deposit"
WITHDRAWAL = "Withdrawal"
TRANSFER_IN = "Transfer In"
TRANSFER_OUT = "Transfer Out"
SPLIT = "Split"
REVERSE_SPLIT = "Reverse Split"
STOCK_DIVIDEND = "Stock Dividend"
SPIN_OFF = "Spin-off"
CASH_IN_LIEU = "Cash-in-lieu"
REDEMPTION = "Redemption"
FX_CONVERSION = "FX Conversion"

EVENT_TYPES = frozenset(
    {
        BUY,
        SELL,
        DIVIDEND,
        COUPON,
        INTEREST,
        FEE,
        DEPOSIT,
        WITHDRAWAL,
        TRANSFER_IN,
        TRANSFER_OUT,
        SPLIT,
        REVERSE_SPLIT,
        STOCK_DIVIDEND,
        SPIN_OFF,
        CASH_IN_LIEU,
        REDEMPTION,
        FX_CONVERSION,
    }
)

SECURITY_EVENTS = frozenset(
    {
        BUY,
        SELL,
        DIVIDEND,
        COUPON,
        TRANSFER_IN,
        TRANSFER_OUT,
        SPLIT,
        REVERSE_SPLIT,
        STOCK_DIVIDEND,
        SPIN_OFF,
        CASH_IN_LIEU,
        REDEMPTION,
    }
)
LOT_EVENTS = frozenset(
    {
        BUY,
        SELL,
        TRANSFER_IN,
        TRANSFER_OUT,
        SPLIT,
        REVERSE_SPLIT,
        STOCK_DIVIDEND,
        SPIN_OFF,
        CASH_IN_LIEU,
        REDEMPTION,
    }
)
INCOME_EVENTS = frozenset({DIVIDEND, COUPON, INTEREST})
CASH_EVENTS = frozenset({DEPOSIT, WITHDRAWAL, FEE, FX_CONVERSION})


@dataclass
class Event:
    type: str
    date: date
    account: str
    currency: str
    security: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None  # per unit, in `currency` (clean price for bonds)
    amount: Decimal | None = None  # cash-type events: Deposit/Withdrawal/Fee/FX leg
    gross: Decimal | None = None  # income events: gross before withholding
    fees: Decimal = Decimal(0)
    taxes: Decimal = Decimal(0)  # withholding tax / transaction taxes
    accrued_interest: Decimal = Decimal(0)  # bonds: paid on Buy, received on Sell
    split_ratio: Decimal | None = None  # new shares per old share (Split 4, Reverse 0.25)
    basis_allocation: Decimal | None = None  # Spin-off: fraction of parent basis to child
    child_security: str | None = None  # Spin-off: the distributed security
    child_ratio: Decimal | None = None  # Spin-off/Stock Dividend: child qty per parent share
    target_currency: str | None = None  # FX Conversion
    target_amount: Decimal | None = None  # FX Conversion
    lot_ids: tuple = field(default_factory=tuple)  # Specific-ID sells
    source: str = "Manual"
    source_ref: str = ""
    notes: str = ""
    meta: dict = field(default_factory=dict)

    def validate(self):
        if self.type not in EVENT_TYPES:
            raise ValueError(f"Unknown investment event type: {self.type}")
        if self.type in SECURITY_EVENTS and not self.security:
            raise ValueError(f"{self.type} requires a security.")
        if self.type in {BUY, SELL, TRANSFER_IN, TRANSFER_OUT, CASH_IN_LIEU, REDEMPTION}:
            if self.qty is None or self.qty <= 0:
                raise ValueError(f"{self.type} requires a positive quantity.")
            if self.type in {BUY, SELL, CASH_IN_LIEU, REDEMPTION} and (self.price is None or self.price < 0):
                raise ValueError(f"{self.type} requires a non-negative price.")
        if self.type in {DIVIDEND, COUPON, INTEREST}:
            if self.gross is None or self.gross < 0:
                raise ValueError(f"{self.type} requires a non-negative gross amount.")
        if self.type in CASH_EVENTS and (self.amount is None or self.amount <= 0):
            raise ValueError(f"{self.type} requires a positive amount.")
        if self.type in {SPLIT, REVERSE_SPLIT, STOCK_DIVIDEND}:
            if self.split_ratio is None or self.split_ratio <= 0:
                raise ValueError(f"{self.type} requires a positive ratio.")
        if self.type == REVERSE_SPLIT and self.split_ratio >= 1:
            raise ValueError("Reverse Split ratio must be below 1.")
        if self.type == SPLIT and self.split_ratio <= 1:
            raise ValueError("Split ratio must be above 1.")
        if self.type == SPIN_OFF:
            if not self.child_security:
                raise ValueError("Spin-off requires a child security.")
            if self.basis_allocation is None or not 0 < self.basis_allocation < 1:
                raise ValueError("Spin-off requires a basis allocation between 0 and 1.")
            if self.child_ratio is None or self.child_ratio <= 0:
                raise ValueError("Spin-off requires a positive child ratio.")
        if self.type == SELL and self.lot_ids and not isinstance(self.lot_ids, (list, tuple)):
            raise ValueError("lot_ids must be a list of lot identifiers.")
        if self.type == FX_CONVERSION and (not self.target_currency or self.target_amount is None):
            raise ValueError("FX Conversion requires target currency and amount.")
        return self

    def net_income(self):
        """Income events: cash actually received."""
        if self.type not in INCOME_EVENTS:
            raise ValueError("net_income only applies to income events")
        return dec(self.gross) - dec(self.taxes) - dec(self.fees)

    def cash_flow(self):
        t = self.type
        if t == BUY:
            return -(dec(self.qty) * dec(self.price) + dec(self.fees) + dec(self.accrued_interest))
        if t in {SELL, CASH_IN_LIEU, REDEMPTION}:
            return (
                dec(self.qty) * dec(self.price)
                - dec(self.fees)
                - dec(self.taxes)
                + dec(self.accrued_interest)
            )
        if t in INCOME_EVENTS:
            return self.net_income()
        if t in {DEPOSIT, TRANSFER_IN} and not self.security:
            return dec(self.amount)
        if t in {WITHDRAWAL, TRANSFER_OUT} and not self.security:
            return -dec(self.amount)
        if t == FEE:
            return -dec(self.amount)
        if t == FX_CONVERSION:
            return -dec(self.amount)  # source leg; target leg is a separate currency line
        return Decimal(0)  # splits/spin-offs move no cash
