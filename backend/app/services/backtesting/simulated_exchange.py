"""Simulated venue state: visible books, open orders, queue evidence, settlement.

All matching is against books whose ``available_at`` is not after the arrival
instant of the order. Equal-instant market events are applied before order and
cancel arrivals (the venue moved first), which is the conservative race rule.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.services.analytics.replay.ledger import PaperLedger
from app.services.backtesting.contracts import EngineConfig, OrderIntent, ReplayEvent
from app.services.backtesting.fill_simulator import round_price, round_shares, walk
from app.services.backtesting.transaction_cost_model import CostModel

ZERO = Decimal(0)


@dataclass
class OrderState:
    intent: OrderIntent
    decided_at: datetime
    arrives_at: datetime
    status: str = "in_flight"
    reason: str | None = None
    requested: Decimal = ZERO
    filled: Decimal = ZERO
    resting: Decimal = ZERO
    limit_price: Decimal | None = None
    rest_id: str | None = None
    queue_ahead: Decimal | None = None
    queue_known: bool = False
    fills: list[dict] = field(default_factory=list)
    uncertain: list[dict] = field(default_factory=list)
    arrived_at: datetime | None = None
    book_event_id: str | None = None
    depth_quality: str | None = None
    final: bool = False

    def view(self) -> dict:
        return {
            "intent_id": self.intent.intent_id,
            "token_id": self.intent.token_id,
            "side": self.intent.side,
            "order_type": self.intent.order_type,
            "decided_at": self.decided_at.isoformat(),
            "arrives_at": self.arrives_at.isoformat(),
            "status": self.status,
            "reason": self.reason,
            "requested_shares": str(self.requested),
            "filled_shares": str(self.filled),
            "resting_shares": str(self.resting),
            "limit_price": None if self.limit_price is None else str(self.limit_price),
            "book_event_id": self.book_event_id,
            "depth_quality": self.depth_quality,
            "queue_evidence": "observed_depth" if self.queue_known else "none",
        }


class SimulatedExchange:
    def __init__(self, config: EngineConfig):
        self.config = config
        self.costs = CostModel(config)
        self.ledger = PaperLedger(config.funding)
        self.books: dict[str, ReplayEvent] = {}
        self.orders: dict[str, OrderState] = {}
        self.fills: list[dict] = []
        self.settled: dict[str, dict] = {}
        self.disputed: set[str] = set()
        self.unconfirmed: list[str] = []
        self.unknown_fee_fills = 0
        self.approximate_fills = 0
        self.settlement_cash = ZERO
        self._fill_seq = 0

    # ----- market state -------------------------------------------------
    def apply_book(self, event: ReplayEvent) -> None:
        current = self.books.get(event.token_id)
        if current is None or (event.available_at, event.sequence) >= (
            current.available_at,
            current.sequence,
        ):
            self.books[event.token_id] = event

    def visible_book(self, token: str, now: datetime) -> ReplayEvent | None:
        book = self.books.get(token)
        if book is None or book.available_at > now:
            return None
        if now - book.available_at > timedelta(seconds=self.config.max_book_age_seconds):
            return None
        return book

    def mark(self, token: str) -> Decimal | None:
        book = self.books.get(token)
        if book is None or not book.bids or not book.asks:
            return None
        return (book.bids[0].price + book.asks[0].price) / 2

    # ----- order arrival ------------------------------------------------
    def _reject(self, order: OrderState, reason: str) -> None:
        order.status, order.reason, order.final = f"rejected_{reason}", reason, True

    def arrive(self, order: OrderState, now: datetime) -> None:
        intent, config = order.intent, self.config
        order.arrived_at = now
        token = intent.token_id
        if token in self.settled:
            return self._reject(order, "market_settled")
        if token in self.disputed:
            return self._reject(order, "settlement_disputed")
        book = self.visible_book(token, now)
        if book is None:
            return self._reject(order, "no_fresh_book")
        order.book_event_id = book.event_id
        buy = intent.side == "BUY"
        opposite = book.asks if buy else book.bids
        own = book.bids if buy else book.asks
        if not opposite and intent.order_type == "IOC":
            return self._reject(order, "empty_book_side")

        limit = intent.limit_price
        if limit is None:
            if not opposite:
                return self._reject(order, "no_reference_price")
            limit = opposite[0].price
        limit = round_price(limit, intent.side, config.tick)
        if not 0 < limit < 1:
            return self._reject(order, "limit_outside_price_range")
        order.limit_price = limit

        taker_known = self.costs.taker_known
        fee_bps = self.costs.ledger_bps("taker")
        budget = None
        if intent.units == "USDC":
            if not buy:
                if not opposite:
                    return self._reject(order, "no_reference_price")
                quantity = round_shares(intent.amount / opposite[0].price, config.lot)
            else:
                budget = min(intent.amount, self.ledger.available_cash)
                reference = opposite[0].price if opposite else limit
                quantity = round_shares(budget / (reference * (1 + fee_bps / 10000)), config.lot)
        else:
            quantity = round_shares(intent.amount, config.lot)
        if buy and budget is None:
            budget = self.ledger.available_cash
        if not buy:
            quantity = min(quantity, self.ledger.available_shares(token))
        order.requested = quantity
        if quantity < config.minimum_shares:
            reason = (
                "insufficient_collateral"
                if (buy and budget is not None and budget <= 0)
                or (not buy and self.ledger.available_shares(token) <= 0)
                else "below_minimum_size"
            )
            return self._reject(order, reason)

        result = walk(
            opposite,
            intent.side,
            quantity,
            limit,
            config.lot,
            config.approximation_max_shares,
            fee_bps if buy else None,
            budget if buy else None,
        )
        taker_qty = result.shares
        if taker_qty > 0:
            worst = max(p for p, _ in result.fills) if buy else min(p for p, _ in result.fills)
            self.ledger.reserve(intent.intent_id, token, intent.side, taker_qty, worst, fee_bps)
            order.depth_quality = result.depth_quality
            for price, shares in result.fills:
                self._record_fill(order, intent.intent_id, shares, price, now, "taker", taker_known)
                if result.depth_quality != "displayed_depth":
                    self.approximate_fills += 1
        order.filled = taker_qty
        remaining = quantity - taker_qty
        if remaining <= 0:
            order.status, order.final = "filled", True
            return None
        if intent.order_type == "IOC":
            order.status = "partial_ioc_cancelled" if taker_qty else "unfilled_ioc"
            order.reason = "displayed_depth_exhausted" if taker_qty or opposite else "no_liquidity"
            order.final = True
            return None
        # GTC remainder rests. Fills need observed order flow, see apply_trade.
        rest_id = f"{intent.intent_id}:rest"
        rest_fee = self.costs.ledger_bps("maker")
        if buy:
            cost_each = limit * (1 + rest_fee / 10000)
            affordable = round_shares(self.ledger.available_cash / cost_each, config.lot)
            remaining = min(remaining, affordable)
        if remaining < config.lot or (buy and remaining <= 0):
            order.status = (
                "partial_then_collateral_exhausted"
                if taker_qty
                else "rejected_insufficient_collateral"
            )
            order.reason, order.final = "insufficient_collateral", True
            return None
        self.ledger.reserve(rest_id, token, intent.side, remaining, limit, rest_fee)
        order.rest_id, order.resting = rest_id, remaining
        order.queue_known = all(
            level.shares is not None for level in own if level.price == limit
        ) and any(level.price == limit for level in own)
        ahead = [level.shares for level in own if level.price == limit]
        order.queue_ahead = ahead[0] if order.queue_known else None
        order.status = "resting_partial" if taker_qty else "resting"
        order.reason = "maker_fills_require_observed_order_flow"
        return None

    # ----- observed order flow -------------------------------------------
    def apply_trade(self, event: ReplayEvent, now: datetime) -> None:
        for order in self.orders.values():
            if order.final or order.resting <= 0 or order.intent.token_id != event.token_id:
                continue
            if order.arrived_at is None or order.arrived_at > now:
                continue
            buy = order.intent.side == "BUY"
            if (buy and event.taker_side != "SELL") or (not buy and event.taker_side != "BUY"):
                continue
            limit = order.limit_price
            through = event.price < limit if buy else event.price > limit
            at_price = event.price == limit
            if not through and not at_price:
                continue
            available = event.shares
            if at_price:
                if not order.queue_known:
                    # Price was touched but our queue position is unknowable.
                    order.uncertain.append(
                        {
                            "event_id": event.event_id,
                            "shares": str(min(order.resting, available)),
                            "reason": "maker_fill_uncertain_no_queue_evidence",
                        }
                    )
                    continue
                consumed = min(order.queue_ahead, available)
                order.queue_ahead -= consumed
                available -= consumed
            take = min(order.resting, round_shares(available, self.config.lot))
            if take <= 0:
                continue
            self._record_fill(
                order, order.rest_id, take, limit, now, "maker", self.costs.maker_known
            )
            order.resting -= take
            order.filled += take
            if order.resting <= 0:
                order.status, order.final, order.reason = (
                    "filled",
                    True,
                    "maker_filled_by_observed_flow",
                )
            else:
                order.status = "resting_partial"

    def _record_fill(self, order, ledger_id, shares, price, now, liquidity, fee_known) -> None:
        self._fill_seq += 1
        fill_id = f"fill:{order.intent.intent_id}:{self._fill_seq}"
        self.ledger.fill(fill_id, ledger_id, shares, price)
        bps = self.costs.ledger_bps(liquidity)
        fee = shares * price * bps / 10000
        if not fee_known:
            self.unknown_fee_fills += 1
        record = {
            "fill_id": fill_id,
            "order_id": order.intent.intent_id,
            "token_id": order.intent.token_id,
            "side": order.intent.side,
            "price": str(price),
            "shares": str(shares),
            "fee": str(fee) if fee_known else None,
            "fee_status": "modeled" if fee_known else "unknown",
            "liquidity": liquidity,
            "fill_time": now.isoformat(),
            "depth_quality": order.depth_quality if liquidity == "taker" else "observed_trade_flow",
        }
        order.fills.append(record)
        self.fills.append(record)

    # ----- cancel / expiry --------------------------------------------------
    def cancel(self, intent_id: str) -> dict:
        order = self.orders.get(intent_id)
        if order is None:
            return {"intent_id": intent_id, "result": "rejected_unknown_order"}
        if order.final or order.status == "in_flight":
            # In flight: the cancel reaches the venue before the order does.
            if order.status == "in_flight":
                order.status, order.reason, order.final = (
                    "cancelled_before_arrival",
                    "cancel_won_race",
                    True,
                )
                return {"intent_id": intent_id, "result": "cancelled_before_arrival"}
            return {
                "intent_id": intent_id,
                "result": "rejected_too_late",
                "order_status": order.status,
            }
        self._cancel_resting(order, "cancelled", "cancel_accepted")
        return {"intent_id": intent_id, "result": "cancelled"}

    def expire(self, intent_id: str) -> None:
        order = self.orders.get(intent_id)
        if order is not None and not order.final and order.resting > 0:
            self._cancel_resting(order, "expired", "gtc_expired")

    def _cancel_resting(self, order: OrderState, status: str, reason: str) -> None:
        if order.rest_id and order.rest_id in self.ledger.orders:
            self.ledger.cancel(f"cancel:{order.rest_id}", order.rest_id)
        order.resting = ZERO
        order.status = status if not order.filled else f"{status}_after_partial"
        order.reason, order.final = reason, True

    # ----- settlement -------------------------------------------------------
    def settle(self, event: ReplayEvent) -> None:
        token = event.token_id
        if event.status == "DISPUTED":
            self.disputed.add(token)
            return
        if event.status == "PENDING":
            return
        if not event.confirmed:
            self.unconfirmed.append(event.event_id)
            return
        if token in self.settled:
            return
        self.disputed.discard(token)
        for order in self.orders.values():
            if order.intent.token_id == token and not order.final and order.resting > 0:
                self._cancel_resting(order, "cancelled", "cancelled_by_settlement")
        before = self.ledger.cash
        shares = self.ledger.positions.get(token).shares if token in self.ledger.positions else ZERO
        self.ledger.settle(f"settle:{event.event_id}", token, event.payout)
        credited = self.ledger.cash - before
        self.settlement_cash += credited
        self.settled[token] = {
            "event_id": event.event_id,
            "status": event.status,
            "payout": str(event.payout),
            "shares": str(shares),
            "cash_credited": str(credited),
        }

    # ----- reporting --------------------------------------------------------
    def marks(self) -> dict[str, Decimal]:
        found = {}
        for token, position in self.ledger.positions.items():
            if position.shares and token not in self.settled:
                mark = self.mark(token)
                if mark is not None:
                    found[token] = mark
        return found

    def valuation(self) -> dict:
        return self.ledger.value(self.marks())
