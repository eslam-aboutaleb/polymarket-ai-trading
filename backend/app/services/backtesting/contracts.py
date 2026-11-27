"""Frozen contracts for the event-driven replay engine. Money is Decimal."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENGINE_VERSION = "event-replay-v1"

Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")]
Price = Annotated[Decimal, Field(gt=0, lt=1, max_digits=20, decimal_places=8)]
Shares = Annotated[Decimal, Field(gt=0, le=1_000_000_000, max_digits=28, decimal_places=8)]
Bps = Annotated[Decimal, Field(ge=0, le=10000, decimal_places=8)]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class BookLevel(Frozen):
    price: Price
    # None means the capture recorded a price but no size: depth is unknown.
    shares: Annotated[Decimal, Field(ge=0, le=1_000_000_000, max_digits=28)] | None = None


class ReplayEvent(Frozen):
    """An immutable observation.

    ``event_time`` is when it happened at the source; ``available_at`` is when
    our system could first know it. Only ``available_at`` gates visibility, so
    a late-delivered observation can never inform an earlier decision.
    """

    event_id: Identifier
    kind: Literal["BOOK", "TRADE", "SIGNAL", "SETTLEMENT"]
    event_time: datetime
    available_at: datetime
    sequence: int = Field(0, ge=0)
    token_id: Identifier
    condition_id: Identifier = "unknown"
    group_id: Identifier | None = None
    # BOOK
    bids: list[BookLevel] = Field(default_factory=list, max_length=200)
    asks: list[BookLevel] = Field(default_factory=list, max_length=200)
    # TRADE (observed order flow). taker_side is the aggressor's side.
    price: Price | None = None
    shares: Shares | None = None
    taker_side: Literal["BUY", "SELL"] | None = None
    # SIGNAL (a leader's trade, or a model signal). source_pnl is deliberately
    # accepted and ignored: follower P&L is rebuilt from follower fills.
    side: Literal["BUY", "SELL"] | None = None
    amount: Shares | None = None
    units: Literal["SHARES", "USDC"] = "USDC"
    limit_price: Price | None = None
    source_pnl: Decimal | None = None
    # SETTLEMENT
    status: Literal["PENDING", "DISPUTED", "FINAL", "VOID"] | None = None
    payout: Annotated[Decimal, Field(ge=0, le=1)] | None = None
    confirmed: bool = False

    @model_validator(mode="after")
    def validate_event(self):
        if self.event_time.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("UTC-aware timestamps required")
        if self.event_time > self.available_at:
            raise ValueError("event_time cannot be later than available_at")
        if self.kind == "BOOK":
            for levels, rev in ((self.bids, True), (self.asks, False)):
                prices = [level.price for level in levels]
                if prices != sorted(prices, reverse=rev) or len(set(prices)) != len(prices):
                    raise ValueError("book levels must be unique and sorted best first")
            if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
                raise ValueError("crossed book")
        if self.kind == "TRADE" and (
            self.price is None or self.shares is None or not self.taker_side
        ):
            raise ValueError("trade needs price, shares and taker_side")
        if self.kind == "SIGNAL" and (self.side is None or self.amount is None):
            raise ValueError("signal needs side and amount")
        if self.kind == "SETTLEMENT":
            if self.status is None:
                raise ValueError("settlement needs a status")
            if self.status in {"FINAL", "VOID"} and self.confirmed and self.payout is None:
                raise ValueError("confirmed final settlement needs a payout")
            if self.status in {"PENDING", "DISPUTED"} and self.payout is not None:
                raise ValueError("provisional settlement cannot carry a payout")
        return self


class OrderIntent(Frozen):
    """What a strategy asks for. Latency and the venue decide what happens."""

    intent_id: Identifier
    token_id: Identifier
    side: Literal["BUY", "SELL"]
    amount: Shares
    units: Literal["SHARES", "USDC"] = "SHARES"
    limit_price: Price | None = None
    order_type: Literal["IOC", "GTC"] = "IOC"
    expire_after_ms: int | None = Field(None, ge=0)


class CancelIntent(Frozen):
    intent_id: Identifier
    cancel_after_ms: int = Field(0, ge=0)


class EngineConfig(Frozen):
    funding: Annotated[Decimal, Field(ge=0, le=1_000_000_000)] = Decimal(1000)
    decision_latency_ms: int = Field(0, ge=0, le=86_400_000)
    submission_latency_ms: int = Field(0, ge=0, le=86_400_000)
    latency_jitter_ms: int = Field(0, ge=0, le=60_000)
    seed: int = 0
    tick: Decimal = Decimal("0.01")
    lot: Decimal = Decimal("0.01")
    minimum_shares: Decimal = Decimal("5")
    max_book_age_seconds: int = Field(300, ge=1, le=86_400)
    # Unknown cost data stays unknown: None never means zero in the report.
    taker_fee_bps: Bps | None = None
    maker_fee_bps: Bps | None = None
    fee_schedule_id: str | None = Field(None, max_length=80)
    # Size assumed fillable when a capture has prices but no depth. Using it
    # always labels the fill an approximation.
    approximation_max_shares: Shares = Decimal(5)

    @model_validator(mode="after")
    def validate_config(self):
        if self.tick not in {Decimal(x) for x in ("0.1", "0.01", "0.001", "0.0001")}:
            raise ValueError("unsupported tick")
        if (self.taker_fee_bps is None) != (self.fee_schedule_id is None):
            raise ValueError("fee_schedule_id and taker_fee_bps must be supplied together")
        return self
