"""Limit-then-take execution (issue #157).

Post a resting maker limit at the touch, wait up to a timeout, cancel, then
fall back to a taker order ONLY if the net edge still exceeds costs.

Safety properties (all tested):

* Every leg uses a deterministic idempotency key derived from the decision, so a
  retry after a timeout re-reads the same order instead of creating a second one.
  The durable gateway enforces the same key/fingerprint rule.
* The taker leg is sent only after the maker leg is proven terminal. If the
  cancel or the status cannot be confirmed the outcome is ``UNRESOLVED_ORDER_STATE``
  and nothing more is sent (the gateway also blocks new orders while one is
  unresolved).
* A missing book, an unknown fee rate or an edge at or below ``min_net_edge``
  never triggers the taker leg; the decision is recorded as missed.

The module is venue-agnostic (``Venue`` protocol). ``GatewayVenue`` binds it to
``place_live_order`` / ``reconcile_order`` so every order still passes the risk,
halt and live-policy checks. It is off by default: ``LIMIT_THEN_TAKE_ENABLED``.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Literal, Protocol

ZERO = Decimal(0)
TERMINAL = ("filled", "cancelled", "rejected")
CENT = Decimal("0.01")


class VenueTimeout(Exception):
    """The venue call may or may not have been applied. Retry with the SAME key."""


@dataclass(frozen=True)
class OrderState:
    state: Literal["open", "partial", "filled", "cancelled", "rejected", "unknown"]
    filled_shares: Decimal = ZERO
    avg_price: Decimal | None = None


@dataclass(frozen=True)
class Book:
    best_bid: Decimal | None
    best_ask: Decimal | None
    tick: Decimal = Decimal("0.01")


class Venue(Protocol):
    def book(self, token_id: str) -> Book: ...
    def place(
        self,
        *,
        key: str,
        token_id: str,
        side: str,
        price: Decimal,
        shares: Decimal,
        order_type: str,
        post_only: bool,
    ) -> OrderState: ...
    def status(self, key: str) -> OrderState: ...
    def cancel(self, key: str) -> OrderState: ...


@dataclass(frozen=True)
class Params:
    timeout_seconds: float = 20.0
    poll_seconds: float = 1.0
    improve_ticks: int = 0  # 0 = join the touch; 1 = one tick inside, never crossing
    min_net_edge: Decimal = ZERO  # per share, after the taker fee
    max_take_slippage: Decimal = Decimal("0.015")  # vs decision price, as a fraction
    taker_fee_rate: Decimal | None = None  # None = unknown: never take
    min_take_shares: Decimal = Decimal("0.01")
    retries: int = 3


@dataclass(frozen=True)
class Decision:
    decision_id: str
    strategy: str
    token_id: str
    side: Literal["BUY", "SELL"]
    shares: Decimal
    decision_price: Decimal  # reference price when the decision was made
    fair_value: Decimal  # expected value per share used for the net-edge check


@dataclass
class Execution:
    decision_id: str
    outcome: str
    maker_filled: Decimal = ZERO
    maker_price: Decimal | None = None
    taker_filled: Decimal = ZERO
    taker_price: Decimal | None = None
    fees: Decimal = ZERO
    final_ref_price: Decimal | None = None
    keys: list[str] = field(default_factory=list)
    reason: str | None = None

    @property
    def filled(self) -> Decimal:
        return self.maker_filled + self.taker_filled

    @property
    def avg_price(self) -> Decimal | None:
        if not self.filled:
            return None
        spent = self.maker_filled * (self.maker_price or ZERO)
        spent += self.taker_filled * (self.taker_price or ZERO)
        return spent / self.filled


def order_key(decision: Decision, leg: str) -> str:
    """Deterministic, <= 200 chars: the same decision and leg always map to one key."""
    digest = hashlib.sha256(
        "|".join(
            [
                decision.strategy,
                decision.decision_id,
                decision.token_id,
                decision.side,
                str(decision.shares.normalize()),
                leg,
            ]
        ).encode()
    ).hexdigest()
    return f"ltt:{decision.strategy[:40]}:{leg}:{digest[:40]}"


def taker_fee(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    """Polymarket taker fee: shares * rate * p * (1 - p). Makers pay nothing."""
    return shares * rate * price * (1 - price)


def net_edge_per_share(decision: Decision, take_price: Decimal, rate: Decimal) -> Decimal:
    fee = rate * take_price * (1 - take_price)
    if decision.side == "BUY":
        return decision.fair_value - take_price - fee
    return take_price - decision.fair_value - fee


def maker_price(side: str, book: Book, improve_ticks: int) -> Decimal | None:
    """Price at the touch that can rest (never crosses the spread); None without a book."""
    if not book.best_bid or not book.best_ask or book.best_bid >= book.best_ask:
        return None
    tick = book.tick
    if side == "BUY":
        price = book.best_bid + improve_ticks * tick
        return min(price, book.best_ask - tick) if improve_ticks else book.best_bid
    price = book.best_ask - improve_ticks * tick
    return max(price, book.best_bid + tick) if improve_ticks else book.best_ask


def _retry(call: Callable[[], OrderState], attempts: int) -> OrderState:
    """Retry timeouts with the same call (same key). Exhausted retries mean unknown."""
    for _ in range(max(1, attempts)):
        try:
            return call()
        except VenueTimeout:
            continue
    return OrderState("unknown")


def run_limit_then_take(
    decision: Decision,
    params: Params,
    venue: Venue,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Execution:
    result = Execution(decision.decision_id, "PENDING")
    if decision.shares <= 0:
        result.outcome, result.reason = "REJECTED", "NON_POSITIVE_SIZE"
        return result
    book = venue.book(decision.token_id)
    price = maker_price(decision.side, book, params.improve_ticks)
    if price is None:
        result.outcome, result.reason = "REJECTED", "NO_USABLE_BOOK"
        return result

    maker_key = order_key(decision, "maker")
    result.keys.append(maker_key)
    shares = decision.shares.quantize(CENT, rounding=ROUND_DOWN)
    state = _retry(
        lambda: venue.place(
            key=maker_key,
            token_id=decision.token_id,
            side=decision.side,
            price=price,
            shares=shares,
            order_type="GTC",
            post_only=True,
        ),
        params.retries,
    )
    deadline = clock() + params.timeout_seconds
    while state.state in ("open", "partial", "unknown") and clock() < deadline:
        sleep(params.poll_seconds)
        state = _retry(lambda: venue.status(maker_key), params.retries)
    if state.state not in TERMINAL:
        _retry(lambda: venue.cancel(maker_key), params.retries)
        state = _retry(lambda: venue.status(maker_key), params.retries)
    if state.state not in TERMINAL:
        result.outcome, result.reason = "UNRESOLVED_ORDER_STATE", "MAKER_CANCEL_UNCONFIRMED"
        return result
    result.maker_filled = state.filled_shares
    result.maker_price = state.avg_price or price

    remaining = (decision.shares - result.maker_filled).quantize(CENT, rounding=ROUND_DOWN)
    fresh = venue.book(decision.token_id)
    result.final_ref_price = (
        (fresh.best_bid + fresh.best_ask) / 2 if fresh.best_bid and fresh.best_ask else None
    )
    if remaining < params.min_take_shares:
        result.outcome = "FILLED_MAKER" if result.maker_filled else "MISSED_NO_REMAINING"
        return result

    take_price = fresh.best_ask if decision.side == "BUY" else fresh.best_bid
    reason = None
    if params.taker_fee_rate is None:
        reason = "UNKNOWN_TAKER_FEE"
    elif not take_price:
        reason = "NO_USABLE_BOOK"
    elif abs(take_price - decision.decision_price) / decision.decision_price > (
        params.max_take_slippage
    ):
        reason = "SLIPPAGE_GUARD"
    elif net_edge_per_share(decision, take_price, params.taker_fee_rate) <= params.min_net_edge:
        reason = "EDGE_GONE"
    if reason:
        result.outcome = "MIXED_PARTIAL_MISSED" if result.maker_filled else "MISSED_" + reason
        result.reason = reason
        return result

    take_key = order_key(decision, "take")
    result.keys.append(take_key)
    take = _retry(
        lambda: venue.place(
            key=take_key,
            token_id=decision.token_id,
            side=decision.side,
            price=take_price,
            shares=remaining,
            order_type="FOK",
            post_only=False,
        ),
        params.retries,
    )
    if take.state in ("open", "partial", "unknown"):
        take = _retry(lambda: venue.status(take_key), params.retries)
    if take.state not in TERMINAL:
        result.outcome, result.reason = "UNRESOLVED_ORDER_STATE", "TAKE_STATE_UNCONFIRMED"
        return result
    result.taker_filled = take.filled_shares
    result.taker_price = take.avg_price or take_price
    if take.filled_shares:
        result.fees = taker_fee(take.filled_shares, result.taker_price, params.taker_fee_rate)
    filled_all = result.filled >= decision.shares - CENT
    if result.maker_filled and result.taker_filled:
        result.outcome = "FILLED_MIXED" if filled_all else "MIXED_PARTIAL_MISSED"
    elif result.taker_filled:
        result.outcome = "FILLED_TAKER" if filled_all else "TAKER_PARTIAL"
    else:
        result.outcome = "MIXED_PARTIAL_MISSED" if result.maker_filled else "MISSED_FOK_KILLED"
        result.reason = "FOK_NOT_FILLED"
    return result


# ───────────── durable gateway binding ─────────────

_STATE = {
    "submitted": "open",
    "partially_filled": "partial",
    "filled": "filled",
    "cancelled": "cancelled",
    "rejected": "rejected",
}


class GatewayVenue:
    """Binds the executor to the durable gateway: risk, halt and policy checks all apply."""

    def __init__(self, db, *, user_id, strategy, book_reader, account_id=None, **place_options):
        self.db, self.user_id, self.strategy = db, user_id, strategy
        self.account_id, self._book_reader, self._options = account_id, book_reader, place_options
        self._prices: dict[str, Decimal] = {}

    def book(self, token_id: str) -> Book:
        return self._book_reader(token_id)

    def _convert(
        self, result: dict, key: str, fallback_shares: Decimal | None = None
    ) -> OrderState:
        state = _STATE.get(str(result.get("status", "")).lower(), "unknown")
        notional = result.get("filled_notional")
        price = self._prices.get(key)
        shares = ZERO
        if state == "filled":
            shares = Decimal(str(result.get("shares", fallback_shares or 0)))
        elif notional and price:
            shares = (Decimal(str(notional)) / price).quantize(CENT, rounding=ROUND_DOWN)
        avg = (Decimal(str(notional)) / shares) if notional and shares else price
        return OrderState(state, shares, avg)

    def place(self, *, key, token_id, side, price, shares, order_type, post_only) -> OrderState:
        from app.services.trading.execution.live import place_live_order

        self._prices[key] = price
        try:
            result = place_live_order(
                self.db,
                user_id=self.user_id,
                token_id=token_id,
                side=side,
                price=float(price),
                size=float(shares),
                idempotency_key=key,
                strategy=self.strategy,
                order_type=order_type,
                post_only=post_only,
                account_id=self.account_id,
                **self._options,
            )
        except TimeoutError as exc:
            raise VenueTimeout from exc
        return self._convert(result, key, shares)

    def _hash(self, key: str) -> str:
        from sqlalchemy import select

        from app.models.trading.live_order import LiveOrder

        row = self.db.scalar(
            select(LiveOrder).where(
                LiveOrder.user_id == self.user_id, LiveOrder.idempotency_key == key
            )
        )
        if row is None:
            raise VenueTimeout
        return row.order_hash

    def status(self, key: str) -> OrderState:
        from app.services.trading.execution.live import reconcile_order

        result = reconcile_order(
            self.db,
            user_id=self.user_id,
            order_hash=self._hash(key),
            account_id=self.account_id,
        )
        return self._convert(result, key)

    def cancel(self, key: str) -> OrderState:
        from app.services.trading.execution.live import reconcile_order

        result = reconcile_order(
            self.db,
            user_id=self.user_id,
            order_hash=self._hash(key),
            cancel=True,
            account_id=self.account_id,
        )
        return self._convert(result, key)
