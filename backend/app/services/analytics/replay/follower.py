"""Frozen, user-supplied follower experiments; modeled research, never live fills."""

import hashlib
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT, PaperLedger

Amount = Annotated[Decimal, Field(ge=0, le=1_000_000_000, max_digits=18, decimal_places=8)]
Price = Annotated[Decimal, Field(gt=0, le=1, max_digits=9, decimal_places=8)]
Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")]


class FrozenInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Signal(FrozenInput):
    event_id: Identifier
    token_id: Identifier
    first_seen_at: AwareDatetime
    side: Literal["BUY", "SELL"]
    source_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def source_clock(self):
        if self.source_at is not None and self.source_at > self.first_seen_at:
            raise ValueError("signal source clock cannot exceed first receipt")
        return self


class Level(FrozenInput):
    price: Price
    shares: Amount


class Book(FrozenInput):
    liquidity_id: Identifier | None = None
    snapshot_id: Identifier
    token_id: Identifier
    observed_at: AwareDatetime
    source_at: AwareDatetime
    bids: list[Level] = Field(default_factory=list, max_length=10)
    asks: list[Level] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_book(self):
        if self.source_at > self.observed_at:
            raise ValueError("source clock cannot exceed receipt clock")
        if self.bids != sorted(self.bids, key=lambda x: x.price, reverse=True):
            raise ValueError("bids must be descending")
        if self.asks != sorted(self.asks, key=lambda x: x.price):
            raise ValueError("asks must be ascending")
        if self.bids and self.asks and self.bids[0].price > self.asks[0].price:
            raise ValueError("crossed book")
        for levels in (self.bids, self.asks):
            if len({level.price for level in levels}) != len(levels):
                raise ValueError("duplicate book price")
        return self


class Settlement(FrozenInput):
    event_id: Identifier
    token_id: Identifier
    observed_at: AwareDatetime
    payout: Annotated[Decimal, Field(ge=0, le=1, max_digits=9, decimal_places=8)]


class Dataset(FrozenInput):
    source: str = Field(min_length=1, max_length=50)
    signals: list[Signal] = Field(max_length=50)
    books: list[Book] = Field(max_length=250)
    settlements: list[Settlement] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def identities(self):
        # Duplicate notifications may be retried unchanged, never rewritten.
        for rows, key in (
            (self.signals, "event_id"),
            (self.books, "snapshot_id"),
            (self.settlements, "event_id"),
        ):
            seen = {}
            for row in rows:
                identity = getattr(row, key)
                if identity in seen and seen[identity] != row:
                    raise ValueError("dataset identity conflict")
                seen[identity] = row
        return self


class FollowerExperiment(FrozenInput):
    wallet: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    funding: Annotated[Amount, Field(gt=0)] = Decimal(1000)
    order_notional: Annotated[Amount, Field(gt=0)] = Decimal(10)
    fee_bps: Annotated[Amount, Field(le=10000)] | None = None
    delays_seconds: list[Annotated[int, Field(ge=0, le=86400)]] = Field(
        default=[0, 60, 300, 1800], min_length=1, max_length=4
    )
    max_book_age_seconds: int = Field(default=60, ge=0, le=3600)
    max_wait_seconds: int = Field(default=60, ge=0, le=3600)
    tick_size: Price = Decimal("0.01")
    lot_size: Annotated[Amount, Field(gt=0)] = Decimal("0.01")
    dataset: Dataset

    @model_validator(mode="after")
    def time_range(self):
        if self.start_at >= self.end_at or self.end_at - self.start_at > timedelta(days=366):
            raise ValueError("experiment window must be positive and at most 366 days")
        if len(set(self.delays_seconds)) != len(self.delays_seconds):
            raise ValueError("duplicate delay scenario")
        with localcontext(ACCOUNTING_CONTEXT):
            for book in self.dataset.books:
                if any(level.price % self.tick_size for level in book.bids + book.asks):
                    raise ValueError("book prices must align with the declared tick size")
        return self


def replay(experiment: FollowerExperiment) -> dict:
    """All scenario arithmetic uses a fixed context, independent of host settings."""
    with localcontext(ACCOUNTING_CONTEXT):
        return {
            "model_version": "follower-replay-v2",
            "dataset_hash": hashlib.sha256(
                experiment.dataset.model_dump_json().encode()
            ).hexdigest(),
            "quality": "user_supplied_unverified",
            "execution_eligible": False,
            "baseline": {"name": "no-copy cash", "nav": str(experiment.funding), "net_pnl": "0"},
            "scenarios": [_scenario(experiment, delay) for delay in experiment.delays_seconds],
        }


def _scenario(exp: FollowerExperiment, delay: int) -> dict:
    ledger = PaperLedger(exp.funding)
    outcomes = []
    used = {}
    # Market events retain their time order across both fills and settlement.
    events = []
    seen = set()
    for signal in exp.dataset.signals:
        if signal.event_id in seen or not exp.start_at <= signal.first_seen_at <= exp.end_at:
            continue
        if signal.source_at is not None and signal.source_at < exp.start_at:
            continue
        seen.add(signal.event_id)
        eligible = signal.first_seen_at + timedelta(seconds=delay)
        book = next(
            (
                b
                for b in sorted(exp.dataset.books, key=lambda b: (b.observed_at, b.snapshot_id))
                if b.token_id == signal.token_id
                and eligible
                <= b.observed_at
                <= min(exp.end_at, eligible + timedelta(seconds=exp.max_wait_seconds))
            ),
            None,
        )
        events.append((book.observed_at if book else eligible, 1, signal.event_id, signal, book))
    seen = set()
    for settlement in exp.dataset.settlements:
        if settlement.event_id not in seen and settlement.observed_at <= exp.end_at:
            events.append((settlement.observed_at, 0, settlement.event_id, settlement, None))
            seen.add(settlement.event_id)
    for when, kind, identity, event, book in sorted(events, key=lambda item: item[:3]):
        if kind == 0:
            ledger.settle("settlement:" + identity, event.token_id, event.payout)
            continue
        row = {
            "signal_id": identity,
            "token_id": event.token_id,
            "side": event.side,
            "eligible_at": (event.first_seen_at + timedelta(seconds=delay)).isoformat(),
            "snapshot_id": book.snapshot_id if book else None,
            "shares": "0",
            "status": "unfilled",
        }
        outcomes.append(row)
        if when > exp.end_at or book is None:
            row["reason"] = "missing_eligible_book"
            continue
        if (book.observed_at - book.source_at).total_seconds() > exp.max_book_age_seconds:
            row["reason"] = "stale_book"
            continue
        if exp.fee_bps is None:
            row["reason"] = "missing_fee_policy"
            continue
        levels = book.asks if event.side == "BUY" else book.bids
        if not levels or not any(level.shares for level in levels):
            row["reason"] = "missing_depth"
            continue
        if event.token_id in ledger.settled_tokens:
            row["reason"] = "already_settled"
            continue
        budget = min(exp.order_notional, ledger.available_cash / (1 + exp.fee_bps / 10000))
        remaining = budget if event.side == "BUY" else exp.order_notional
        inventory = ledger.available_shares(event.token_id)
        total_shares = Decimal(0)
        total_cost = Decimal(0)
        for index, level in enumerate(levels):
            if level.price % exp.tick_size:
                row["reason"] = "invalid_tick"
                break
            key = (
                event.token_id,
                book.liquidity_id or book.snapshot_id,
                event.side,
                level.price if book.liquidity_id else index,
            )
            capacity = level.shares - used.get(key, Decimal(0))
            shares = min(capacity, remaining / level.price)
            if event.side == "SELL":
                shares = min(shares, inventory)
            shares = (shares / exp.lot_size).to_integral_value(rounding=ROUND_DOWN) * exp.lot_size
            if shares <= 0:
                continue
            order_id = f"signal:{identity}:{index}"
            ledger.reserve(order_id, event.token_id, event.side, shares, level.price, exp.fee_bps)
            ledger.fill("fill:" + order_id, order_id, shares, level.price)
            used[key] = used.get(key, Decimal(0)) + shares
            total_shares += shares
            total_cost += shares * level.price
            remaining -= shares * level.price
            inventory -= shares
        row.update(shares=str(total_shares), notional=str(total_cost))
        if total_shares:
            row["status"] = "filled" if remaining < exp.lot_size * levels[0].price else "partial"
            row["reason"] = "modeled_depth_fill"
            row["vwap"] = str(total_cost / total_shares)
        else:
            row.setdefault("reason", "insufficient_cash_or_inventory_or_depth")
    marks = {}
    for token, position in ledger.positions.items():
        if not position.shares:
            continue
        books = [
            b for b in exp.dataset.books if b.token_id == token and b.observed_at <= exp.end_at
        ]
        if books:
            latest = max(books, key=lambda b: (b.observed_at, b.snapshot_id))
            positive_bids = [level for level in latest.bids if level.shares > 0]
            if (
                positive_bids
                and (exp.end_at - latest.source_at).total_seconds() <= exp.max_book_age_seconds
            ):
                marks[token] = positive_bids[0].price
    return {
        "delay_seconds": delay,
        "valuation": ledger.value(marks),
        "outcomes": outcomes,
        "journal": ledger.journal,
        "sharpe_ratio": None,
        "mark_policy": "fresh end-of-window best bid; indicative, no exit capacity claim",
    }
