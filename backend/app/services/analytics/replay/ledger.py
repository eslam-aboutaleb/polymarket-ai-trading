"""Deterministic paper accounting. No market, wallet, signer or network dependencies.

All sizes are token shares; all cash, fees and cost basis are USDC. Reserved
cash remains part of total cash and must never be counted twice in NAV.
"""

from copy import copy
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import wraps

ZERO = Decimal(0)
MODEL_VERSION = "paper-ledger-v1"
# Bounded inputs (28 significant digits, exponents -28..28) and a fixed
# context isolate economic results from host precision, rounding and traps.
ACCOUNTING_CONTEXT = Context(prec=256, rounding=ROUND_HALF_EVEN)


def accounting(method):
    @wraps(method)
    def calculate(*args, **kwargs):
        with localcontext(ACCOUNTING_CONTEXT):
            return method(*args, **kwargs)

    return calculate


def atomic(method):
    @wraps(method)
    @accounting
    def commit(self, *args, **kwargs):
        candidate = copy(self)
        candidate.positions = {key: replace(value) for key, value in self.positions.items()}
        candidate.orders = self.orders.copy()
        candidate.journal = self.journal.copy()
        candidate._events = self._events.copy()
        candidate.settled_tokens = self.settled_tokens.copy()
        result = method(candidate, *args, **kwargs)
        self.__dict__.update(candidate.__dict__)
        return result

    return commit


def positive(value: Decimal, name: str, *, zero: bool = False) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or len(value.as_tuple().digits) > 28
        or not -28 <= value.as_tuple().exponent <= 28
        or value < 0
        or (not zero and value == 0)
    ):
        raise ValueError(f"{name} must be a finite {'nonnegative' if zero else 'positive'} Decimal")


@dataclass
class Position:
    shares: Decimal = ZERO
    cost_basis: Decimal = ZERO


@dataclass(frozen=True)
class Reservation:
    token_id: str
    side: str
    shares: Decimal
    limit_price: Decimal
    fee_bps: Decimal


@dataclass
class PaperLedger:
    funding: Decimal
    cash: Decimal = field(init=False)
    fees: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)
    orders: dict[str, Reservation] = field(default_factory=dict)
    journal: list[dict] = field(default_factory=list)
    _events: dict[str, tuple] = field(default_factory=dict)
    settled_tokens: set[str] = field(default_factory=set)

    def __post_init__(self):
        positive(self.funding, "funding")
        self.cash = self.funding

    @property
    @accounting
    def reserved_cash(self) -> Decimal:
        return sum(
            (
                r.shares * r.limit_price * (1 + r.fee_bps / 10000)
                for r in self.orders.values()
                if r.side == "BUY"
            ),
            ZERO,
        )

    @property
    @accounting
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash

    @accounting
    def available_shares(self, token: str) -> Decimal:
        reserved = sum(
            (r.shares for r in self.orders.values() if r.side == "SELL" and r.token_id == token),
            ZERO,
        )
        return self.positions.get(token, Position()).shares - reserved

    def _seen(self, event_id: str, payload: tuple) -> bool:
        if not event_id:
            raise ValueError("event_id required")
        previous = self._events.get(event_id)
        if previous is not None and previous != payload:
            raise ValueError("event identity conflicts with earlier content")
        return previous is not None

    def _record(self, event_id: str, payload: tuple) -> None:
        self._events[event_id] = payload
        self.journal.append(
            {
                "event_id": event_id,
                "kind": payload[0],
                "details": [str(value) for value in payload[1:]],
                "cash": str(self.cash),
                "reserved_cash": str(self.reserved_cash),
                "fees": str(self.fees),
                "realized_pnl": str(self.realized_pnl),
                "positions": {
                    k: {"shares": str(v.shares), "cost_basis": str(v.cost_basis)}
                    for k, v in sorted(self.positions.items())
                },
            }
        )
        if (
            self.cash < 0
            or self.available_cash < 0
            or any(p.shares < 0 or p.cost_basis < 0 for p in self.positions.values())
        ):
            raise AssertionError("paper accounting invariant violated")

    @atomic
    def reserve(
        self,
        event_id: str,
        token: str,
        side: str,
        shares: Decimal,
        limit_price: Decimal,
        fee_bps: Decimal,
    ) -> None:
        payload = ("ORDER_ACCEPTED", token, side, shares, limit_price, fee_bps)
        if self._seen(event_id, payload):
            return
        positive(shares, "shares")
        positive(limit_price, "limit_price")
        positive(fee_bps, "fee_bps", zero=True)
        if not token or token in self.settled_tokens or side not in ("BUY", "SELL"):
            raise ValueError("invalid token or side")
        if limit_price > 1 or fee_bps > 10000:
            raise ValueError("invalid price or fee")
        required = shares * limit_price * (1 + fee_bps / 10000)
        if side == "BUY" and required > self.available_cash:
            raise ValueError("insufficient cash")
        if side == "SELL" and shares > self.available_shares(token):
            raise ValueError("insufficient inventory")
        self.orders[event_id] = Reservation(token, side, shares, limit_price, fee_bps)
        self._record(event_id, payload)

    @atomic
    def fill(self, event_id: str, order_id: str, shares: Decimal, price: Decimal) -> None:
        payload = ("FILL", order_id, shares, price)
        if self._seen(event_id, payload):
            return
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError("order not open")
        positive(shares, "shares")
        positive(price, "price")
        if (
            shares > order.shares
            or price > 1
            or (price > order.limit_price if order.side == "BUY" else price < order.limit_price)
        ):
            raise ValueError("fill exceeds order constraints")
        notional = shares * price
        fee = notional * order.fee_bps / 10000
        position = self.positions.setdefault(order.token_id, Position())
        if order.side == "BUY":
            self.cash -= notional + fee
            position.shares += shares
            position.cost_basis += notional + fee
        else:
            released_basis = (
                position.cost_basis
                if shares == position.shares
                else position.cost_basis * shares / position.shares
            )
            self.cash += notional - fee
            position.shares -= shares
            position.cost_basis -= released_basis
            self.realized_pnl += notional - fee - released_basis
        self.fees += fee
        remaining = order.shares - shares
        if remaining:
            self.orders[order_id] = Reservation(
                order.token_id, order.side, remaining, order.limit_price, order.fee_bps
            )
        else:
            del self.orders[order_id]
        self._record(event_id, payload)

    @atomic
    def cancel(self, event_id: str, order_id: str) -> None:
        payload = ("CANCELED", order_id)
        if self._seen(event_id, payload):
            return
        if order_id not in self.orders:
            raise ValueError("order not open")
        del self.orders[order_id]
        self._record(event_id, payload)

    @atomic
    def settle(self, event_id: str, token: str, payout_per_share: Decimal) -> None:
        payload = ("SETTLED", token, payout_per_share)
        if self._seen(event_id, payload):
            return
        positive(payout_per_share, "payout", zero=True)
        if (
            payout_per_share > 1
            or token in self.settled_tokens
            or any(r.token_id == token for r in self.orders.values())
        ):
            raise ValueError("token already settled or has open orders")
        position = self.positions.get(token, Position())
        payout = position.shares * payout_per_share
        self.cash += payout
        self.realized_pnl += payout - position.cost_basis
        self.positions[token] = Position()
        self.settled_tokens.add(token)
        self._record(event_id, payload)

    @accounting
    def value(self, marks: dict[str, Decimal]) -> dict:
        missing = sorted(k for k, p in self.positions.items() if p.shares and k not in marks)
        for mark in marks.values():
            positive(mark, "mark", zero=True)
            if mark > 1:
                raise ValueError("mark exceeds payout range")
        basis = sum((p.cost_basis for p in self.positions.values()), ZERO)
        gross = sum((p.shares * marks.get(k, ZERO) for k, p in self.positions.items()), ZERO)
        nav = None if missing else self.cash + gross
        return {
            "model_version": MODEL_VERSION,
            "cash": str(self.cash),
            "available_cash": str(self.available_cash),
            "reserved_cash": str(self.reserved_cash),
            "fees": str(self.fees),
            "realized_pnl": str(self.realized_pnl),
            "cost_basis": str(basis),
            "position_value": None if missing else str(gross),
            "unrealized_pnl": None if missing else str(gross - basis),
            "nav": None if nav is None else str(nav),
            "net_pnl": None if nav is None else str(nav - self.funding),
            "missing_marks": missing,
            "positions": {
                k: {"shares": str(p.shares), "cost_basis": str(p.cost_basis)}
                for k, p in sorted(self.positions.items())
                if p.shares
            },
        }
