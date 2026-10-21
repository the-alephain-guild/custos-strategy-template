"""Causal, fee-inclusive fill accounting for explicit linear instrument economics."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

ZERO = Decimal(0)


class Unavailable(ValueError):
    """The observations do not support a complete monetary calculation."""


def decimal(value: object, field: str) -> Decimal:
    try:
        if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
            raise ValueError
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError) as error:
        raise Unavailable(f"Missing or invalid {field}") from error


def text(value: Decimal) -> str:
    return "0" if value == 0 else format(value, "f")


@dataclass(frozen=True)
class Economics:
    currency: str
    precision: int
    multiplier: Decimal

    @classmethod
    def read(cls, raw: dict) -> Economics:
        if raw.get("price_model") != "linear":
            raise Unavailable("Instrument price model is missing or unsupported")
        currency = raw.get("pnl_currency")
        precision = raw.get("pnl_precision", 8)
        multiplier = decimal(raw.get("multiplier"), "contract multiplier")
        if not isinstance(currency, str) or not currency or multiplier <= 0:
            raise Unavailable("Missing instrument currency or positive multiplier")
        if type(precision) is not int or not 0 <= precision <= 18:
            raise Unavailable("Invalid currency precision")
        return cls(currency, precision, multiplier)


def commission(fill: dict, currency: str) -> Decimal:
    if "commission_amount" in fill or "commission_currency" in fill:
        amount, unit = fill.get("commission_amount"), fill.get("commission_currency")
    else:
        raw = fill.get("commission")
        if not isinstance(raw, str) or len(raw.split()) != 2:
            raise Unavailable("Commission amount and currency were not recorded")
        amount, unit = raw.split()
    fee = decimal(amount, "commission amount")
    if not isinstance(unit, str) or not unit:
        raise Unavailable("Commission currency was not recorded")
    if fee != 0 and unit != currency:
        raise Unavailable(f"Commission currency {unit} needs an observed conversion to {currency}")
    return fee


@dataclass
class Book:
    quantity: Decimal = ZERO
    cost: Decimal = ZERO
    entry_fees: Decimal = ZERO
    cumulative: Decimal = ZERO
    error: str | None = None

    def apply(self, fill: dict, terms: Economics) -> dict:
        quantity = decimal(fill.get("quantity"), "fill quantity")
        price = decimal(fill.get("price"), "fill price")
        if quantity <= 0 or price <= 0 or fill.get("side") not in ("BUY", "SELL"):
            raise Unavailable("Fill price and quantity must be positive with a known side")
        change = quantity if fill["side"] == "BUY" else -quantity
        after = self.quantity + change
        for key, expected in (("position_before", self.quantity), ("position_after", after)):
            if key in fill and decimal(fill[key], key) != expected:
                raise Unavailable("Position history is incomplete or inconsistent")
        fee = commission(fill, terms.currency)
        closing = min(abs(self.quantity), quantity) if self.quantity * change < 0 else ZERO
        opening = quantity - closing
        closed_cost = entry_fee = exit_fee = gross = closed_net = ZERO
        return_pct = None
        if closing:
            # Consume the entire remaining pools on a full close, so repeated
            # partial allocations cannot strand fee or cost rounding residue.
            full_close = closing == abs(self.quantity)
            closed_cost = self.cost if full_close else self.cost * closing / abs(self.quantity)
            entry_fee = (
                self.entry_fees if full_close else self.entry_fees * closing / abs(self.quantity)
            )
            exit_fee = fee * closing / quantity
            exit_notional = closing * price * terms.multiplier
            gross = (exit_notional - closed_cost) * (1 if self.quantity > 0 else -1)
            closed_net = gross - entry_fee - exit_fee
            return_pct = closed_net / closed_cost * 100
        self.cost -= closed_cost
        self.entry_fees -= entry_fee
        if opening:
            self.cost += opening * price * terms.multiplier
            self.entry_fees += fee - exit_fee
        self.quantity = after
        if after == 0:
            self.cost = self.entry_fees = ZERO
        net = gross - fee
        self.cumulative += net
        return {
            "status": "ok",
            "currency": terms.currency,
            "currency_precision": terms.precision,
            "realized_gross": text(gross),
            "fee": text(fee),
            "realized_net": text(net),
            "cumulative_net": text(self.cumulative),
            "closed_quantity": text(closing),
            "entry_notional": text(closed_cost),
            "entry_fee_allocated": text(entry_fee),
            "exit_fee_allocated": text(exit_fee),
            "closed_net": text(closed_net) if closing else None,
            "return_pct": text(return_pct) if return_pct is not None else None,
        }


def annotate_fills(fills: list[dict], instruments: list[dict]) -> list[dict]:
    """Copy fills, preserving identities/order, and value each instrument's history."""
    definitions = {}
    for instrument in instruments:
        if instrument["id"] in definitions:
            raise ValueError("duplicate instrument economics")
        definitions[instrument["id"]] = instrument
    if len({fill["id"] for fill in fills}) != len(fills):
        raise ValueError("duplicate fill identity")
    rows = [dict(fill) for fill in fills]
    books: dict[str, Book] = {}
    # Stable ties retain callback order; random native IDs never order fills.
    ordered = sorted(enumerate(rows), key=lambda item: (int(item[1]["event_time_ns"]), item[0]))
    with localcontext() as context:
        context.prec = 50
        for _, row in ordered:
            identity = row["instrument_id"]
            book = books.setdefault(identity, Book())
            raw = definitions.get(identity, {})
            try:
                if book.error:
                    raise Unavailable(book.error)
                terms = Economics.read(raw)
                row["pnl"] = book.apply(row, terms)
            except Unavailable as error:
                # Missing fees or history cannot be treated as zero and then
                # disappear from later cumulative results for this instrument.
                book.error = str(error)
                row["pnl"] = {
                    "status": "unavailable",
                    "currency": raw.get("pnl_currency"),
                    "realized_net": None,
                    "return_pct": None,
                    "cumulative_net": None,
                    "reason": book.error,
                }
    return rows
