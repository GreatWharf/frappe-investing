"""Tax-lot engine. Positions are derived; nothing is ever edited in place.

Supports FIFO / LIFO / AVERAGE / SPECIFIC identification, splits, reverse splits,
stock dividends, spin-offs, cash-in-lieu, transfers and bond redemptions.
All math is Decimal; fees capitalize into cost basis on buys and reduce proceeds on sells.
"""

import itertools
import uuid
from dataclasses import dataclass, field

from .events import (
    BUY,
    CASH_IN_LIEU,
    REDEMPTION,
    REVERSE_SPLIT,
    SELL,
    SPIN_OFF,
    SPLIT,
    STOCK_DIVIDEND,
    TRANSFER_IN,
    TRANSFER_OUT,
)
from .money import dec


class LotError(ValueError):
    pass


_ids = itertools.count(1)


@dataclass
class Lot:
    security: str
    account: str
    qty: object
    unit_cost: object
    currency: str
    acquired: object
    source_ref: str = ""
    id: str = field(default_factory=lambda: f"lot-{uuid.uuid4().hex[:12]}")
    seq: int = field(default_factory=lambda: next(_ids))

    @property
    def cost(self):
        return self.qty * self.unit_cost

    def clone(self, **changes):
        data = dict(
            security=self.security,
            account=self.account,
            qty=self.qty,
            unit_cost=self.unit_cost,
            currency=self.currency,
            acquired=self.acquired,
            source_ref=self.source_ref,
            id=self.id,
            seq=self.seq,
        )
        data.update(changes)
        return Lot(**data)


@dataclass
class Allocation:
    lot_id: str
    qty: object
    cost: object
    proceeds: object
    realized_pnl: object
    acquired: object


@dataclass
class LotResult:
    new_lots: list = field(default_factory=list)
    allocations: list = field(default_factory=list)

    def total_realized_pnl(self):
        return sum((a.realized_pnl for a in self.allocations), dec(0))

    def total_cost(self):
        return sum((lot.cost for lot in self.new_lots), dec(0))


class LotEngine:
    def __init__(self, method="FIFO"):
        if method not in {"FIFO", "LIFO", "AVERAGE", "SPECIFIC"}:
            raise ValueError(f"Unknown cost basis method: {method}")
        self.method = method
        self._lots = {}  # (account, security) -> [Lot]
        self.transferred = {}  # (account, security) -> [Lot] pending transfer-in

    def open_lots(self, account, security):
        return [lot for lot in self._lots.get((account, security), []) if lot.qty > 0]

    def position(self, account, security):
        return sum((lot.qty for lot in self.open_lots(account, security)), dec(0))

    def apply(self, event):
        event.validate()
        handler = {
            BUY: self._buy,
            SELL: self._sell,
            CASH_IN_LIEU: self._sell,
            REDEMPTION: self._sell,
            SPLIT: self._split,
            REVERSE_SPLIT: self._split,
            STOCK_DIVIDEND: self._stock_dividend,
            SPIN_OFF: self._spin_off,
            TRANSFER_OUT: self._transfer_out,
            TRANSFER_IN: self._transfer_in,
        }.get(event.type)
        if handler is None:
            return LotResult()  # income/cash events move no lots
        return handler(event)

    # -- trades ---------------------------------------------------------
    def _buy(self, event):
        qty, price, fees = dec(event.qty), dec(event.price), dec(event.fees)
        key = (event.account, event.security)
        if self.method == "AVERAGE":
            pool = self.open_lots(*key)
            if pool:
                lot = pool[0]
                total_cost = lot.cost + qty * price + fees
                lot.qty += qty
                lot.unit_cost = total_cost / lot.qty
                return LotResult(new_lots=[lot])
        lot = Lot(
            security=event.security,
            account=event.account,
            qty=qty,
            unit_cost=(qty * price + fees) / qty,
            currency=event.currency,
            acquired=event.date,
            source_ref=event.source_ref,
        )
        self._lots.setdefault(key, []).append(lot)
        return LotResult(new_lots=[lot])

    def _sell(self, event):
        qty, price = dec(event.qty), dec(event.price)
        key = (event.account, event.security)
        open_lots = self.open_lots(*key)
        if self.position(*key) < qty:
            raise LotError(f"Insufficient quantity of {event.security} in {event.account}.")
        gross_proceeds = qty * price
        total_cost_reductions = dec(event.fees) + dec(event.taxes)
        if self.method == "SPECIFIC":
            wanted = list(event.lot_ids or [])
            if not wanted:
                raise LotError("Specific identification requires lot_ids on the sell event.")
            chosen = [lot for lot in open_lots if lot.id in wanted]
            if len(chosen) != len(wanted):
                raise LotError("Specific identification requires valid open lots.")
            chosen.sort(key=lambda lot: wanted.index(lot.id))
        elif self.method == "AVERAGE":
            chosen = open_lots[:1]
        else:
            chosen = sorted(open_lots, key=lambda lot: (lot.acquired, lot.seq), reverse=self.method == "LIFO")
        allocations, remaining = [], qty
        for lot in chosen:
            if remaining <= 0:
                break
            take = min(remaining, lot.qty)
            cost = lot.unit_cost * take
            proceeds = gross_proceeds * (take / qty) - total_cost_reductions * (take / qty)
            allocations.append(
                Allocation(
                    lot_id=lot.id,
                    qty=take,
                    cost=cost,
                    proceeds=proceeds,
                    realized_pnl=proceeds - cost,
                    acquired=lot.acquired,
                )
            )
            lot.qty -= take
            remaining -= take
        if remaining > 0:
            raise LotError(f"Selected lots do not cover the {event.type.lower()} quantity.")
        self._lots[key] = [lot for lot in self._lots[key] if lot.qty > 0]
        return LotResult(allocations=allocations)

    # -- corporate actions ------------------------------------------------
    def _split(self, event):
        ratio = dec(event.split_ratio)
        for lot in self.open_lots(event.account, event.security):
            lot.qty *= ratio
            lot.unit_cost /= ratio
        return LotResult(new_lots=self.open_lots(event.account, event.security))

    def _stock_dividend(self, event):
        ratio = dec(event.split_ratio)  # bonus shares per share held
        key = (event.account, event.security)
        result = []
        for lot in self.open_lots(*key):
            if event.meta.get("zero_cost"):
                new = Lot(
                    security=lot.security,
                    account=lot.account,
                    qty=lot.qty * ratio,
                    unit_cost=dec(0),
                    currency=lot.currency,
                    acquired=event.date,
                    source_ref=event.source_ref,
                )
                self._lots[key].append(new)
                result.append(new)
            else:
                # Dilute basis: total cost unchanged, qty grows, unit cost falls.
                lot.qty *= 1 + ratio
                lot.unit_cost /= 1 + ratio
                result.append(lot)
        return LotResult(new_lots=result)

    def _spin_off(self, event):
        alloc = dec(event.basis_allocation)
        ratio = dec(event.child_ratio)
        parent_lots = self.open_lots(event.account, event.security)
        child_lots = []
        for lot in parent_lots:
            child_qty = lot.qty * ratio
            child_cost = lot.cost * alloc
            lot.unit_cost *= 1 - alloc
            child_lots.append(
                Lot(
                    security=event.child_security,
                    account=event.account,
                    qty=child_qty,
                    unit_cost=child_cost / child_qty,
                    currency=lot.currency,
                    acquired=lot.acquired,
                    source_ref=event.source_ref,
                )
            )
        self._lots.setdefault((event.account, event.child_security), []).extend(child_lots)
        return LotResult(new_lots=parent_lots + child_lots)

    # -- transfers --------------------------------------------------------
    def _transfer_out(self, event):
        key = (event.account, event.security)
        qty = dec(event.qty)
        if self.position(*key) < qty:
            raise LotError(f"Insufficient quantity of {event.security} to transfer.")
        moved, remaining = [], qty
        for lot in sorted(self.open_lots(*key), key=lambda item: item.acquired):
            if remaining <= 0:
                break
            take = min(remaining, lot.qty)
            moved.append(lot.clone(qty=take))
            lot.qty -= take
            remaining -= take
        self._lots[key] = [lot for lot in self._lots[key] if lot.qty > 0]
        self.transferred.setdefault(key, []).extend(moved)
        return LotResult(new_lots=moved)

    def _transfer_in(self, event):
        # Transfer In is a manual fallback only: the normal path is a Transfer
        # Out, whose destination Tax Lot rows are written directly by
        # services._persist_transfer_out (same submit, same portfolio). A
        # hand-posted Transfer In would double-count those rows, so it is
        # rejected unless it carries explicit lots (engine-level callers).
        raw = list(event.meta.get("lots", []))
        incoming = [lot.clone(account=event.account) if hasattr(lot, "clone") else Lot(account=event.account, **lot) for lot in raw]
        if not incoming:
            raise LotError("Transfer In requires carried lots (meta['lots']).")
        if sum((lot.qty for lot in incoming), dec(0)) != dec(event.qty):
            raise LotError("Carried lots do not match the transfer quantity.")
        self._lots.setdefault((event.account, event.security), []).extend(incoming)
        return LotResult(new_lots=incoming)
