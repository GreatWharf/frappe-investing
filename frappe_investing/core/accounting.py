"""Investment Accounting Policy → balanced journal-entry lines.

Pure mapping: this module never touches ERPNext. services.py persists the result.
Every event must produce balanced debit == credit lines in company currency, or fail closed.
"""

from dataclasses import dataclass, field

from .events import BUY, CASH_IN_LIEU, FEE, INCOME_EVENTS, REDEMPTION, SELL
from .money import dec, q


class UnmappedEvent(ValueError):
    pass


@dataclass
class Rule:
    debit: str  # main debit account
    credit: str  # main credit account
    tax_debit: str | None = None  # withholding-tax receivable (income events)
    fee_debit: str | None = None  # separate fee expense line
    pnl_account: str | None = None  # realised gain/loss plug on sells


@dataclass
class Policy:
    company_currency: str
    rules: dict = field(default_factory=dict)  # event type -> Rule


@dataclass
class Line:
    account: str
    debit: object
    credit: object
    currency: str | None = None
    amount_in_currency: object | None = None


def _line(account, debit=0, credit=0, currency=None, amount=None):
    return Line(
        account=account,
        debit=q(dec(debit), 2),
        credit=q(dec(credit), 2),
        currency=currency,
        amount_in_currency=None if amount is None else q(dec(amount), 2),
    )


def journal_lines(event, policy, *, rates, on, allocations=None):
    """Build balanced JE lines for an event. allocations come from the lot engine for sells."""
    rule = policy.rules.get(event.type)
    if rule is None:
        raise UnmappedEvent(
            f"No accounting rule for {event.type}. Add it to the Investment Accounting Policy."
        )
    rate = rates.rate(event.currency, policy.company_currency, on)

    def company(amount):
        return q(dec(amount) * rate, 2)

    lines = []
    if event.type == BUY:
        cost = dec(event.qty) * dec(event.price)
        lines.append(_line(rule.debit, company(cost + dec(event.fees)), 0))
        if event.accrued_interest:
            lines.append(_line(rule.fee_debit or rule.debit, company(event.accrued_interest), 0))
        lines.append(_line(rule.credit, 0, company(cost + dec(event.fees) + dec(event.accrued_interest))))
    elif event.type in {SELL, CASH_IN_LIEU, REDEMPTION}:
        proceeds = dec(event.qty) * dec(event.price) - dec(event.fees) - dec(event.taxes)
        cost = sum((a.cost for a in (allocations or [])), dec(0))
        if allocations is None:
            raise UnmappedEvent(f"{event.type} requires lot allocations before accounting.")
        realized = sum((a.realized_pnl for a in allocations), dec(0))
        lines.append(_line(rule.debit, company(proceeds), 0))
        lines.append(_line(rule.credit, 0, company(cost)))
        if realized and rule.pnl_account:
            if realized > 0:
                lines.append(_line(rule.pnl_account, 0, company(realized)))
            else:
                lines.append(_line(rule.pnl_account, company(-realized), 0))
    elif event.type in INCOME_EVENTS:
        net = dec(event.gross) - dec(event.taxes) - dec(event.fees)
        lines.append(_line(rule.debit, company(net), 0))
        if event.taxes and rule.tax_debit:
            lines.append(_line(rule.tax_debit, company(event.taxes), 0))
        elif event.taxes:
            lines.append(
                _line(rule.debit, company(event.taxes), 0)
            )  # expense through cash account when no receivable configured
        lines.append(
            _line(
                rule.credit,
                0,
                company(event.gross) - (company(event.fees) if event.fees and not rule.fee_debit else 0),
            )
        )
        if event.fees and rule.fee_debit:
            lines.append(_line(rule.fee_debit, company(event.fees), 0))
    elif event.type == FEE:
        lines.append(_line(rule.debit, company(event.amount), 0))
        lines.append(_line(rule.credit, 0, company(event.amount)))
    else:
        raise UnmappedEvent(
            f"{event.type} has no journal mapping in v1; cash/transfer/corporate actions post through their cash or trade legs."
        )
    if sum(line.debit for line in lines) != sum(line.credit for line in lines):
        raise UnmappedEvent(f"Accounting mapping for {event.type} produced unbalanced lines.")
    return lines
