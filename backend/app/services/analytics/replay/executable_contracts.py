"""Frozen research contracts. Input claims never become exchange confirmations."""

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from app.services.market_data.binary_book import BookEvent, Contract, MarketSpec

Amount = Annotated[Decimal, Field(gt=0, le=1_000_000_000, max_digits=28, decimal_places=8)]
Bps = Annotated[Decimal, Field(ge=0, le=10000, decimal_places=8)]
Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")]


class ReplaySignal(Contract):
    id: Identifier
    condition_id: str = Field(min_length=1, max_length=200)
    token_id: Identifier
    side: Literal["BUY", "SELL"]
    available_at: AwareDatetime
    source_at: AwareDatetime
    amount: Amount | None = None
    units: Literal["SHARES", "USDC"] = "USDC"
    limit_price: Decimal | None = Field(None, gt=0, lt=1, decimal_places=8)

    @model_validator(mode="after")
    def clocks(self):
        if self.source_at > self.available_at:
            raise ValueError("signal source clock exceeds availability")
        return self


class SettlementEvent(Contract):
    id: Identifier
    condition_id: str = Field(min_length=1, max_length=200)
    token_id: Identifier
    rule_hash: str = Field(min_length=1, max_length=64)
    available_at: AwareDatetime
    source_at: AwareDatetime
    status: Literal["PENDING", "DISPUTED", "EXPIRED", "FINAL", "VOID"]
    payout: Decimal | None = Field(None, ge=0, le=1, decimal_places=8)
    confirmed: bool = False
    source_ids: list[Identifier] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_event(self):
        if self.source_at > self.available_at:
            raise ValueError("settlement source clock exceeds availability")
        if self.status in {"FINAL", "VOID"} and self.confirmed and self.payout is None:
            raise ValueError("final payout required")
        if self.status not in {"FINAL", "VOID"} and self.payout is not None:
            raise ValueError("provisional outcomes cannot carry a payout")
        return self


class ReplayTape(Contract):
    version: Literal["executable-tape-v1"] = "executable-tape-v1"
    specs: list[MarketSpec] = Field(default_factory=list, max_length=100)
    events: list[BookEvent] = Field(default_factory=list, max_length=250)
    signals: list[ReplaySignal] = Field(default_factory=list, max_length=100)
    settlements: list[SettlementEvent] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def identities(self):
        for rows, attribute in (
            (self.specs, "metadata_id"),
            (self.events, "snapshot_id"),
            (self.signals, "id"),
            (self.settlements, "id"),
        ):
            seen = {}
            for row in rows:
                key = getattr(row, attribute)
                if key in seen and seen[key] != row:
                    raise ValueError("conflicting immutable event identity")
                seen[key] = row
        # A token cannot silently move between conditions or YES/NO meanings.
        if sum(len(e.bids) + len(e.asks) + len(e.changes) for e in self.events) > 5000:
            raise ValueError("total captured depth budget exceeded")
        mapping = {}
        for spec in self.specs:
            for token, outcome in ((spec.yes_token, "YES"), (spec.no_token, "NO")):
                value = (spec.condition_id, outcome)
                if token in mapping and mapping[token] != value:
                    raise ValueError("token-swap corruption")
                mapping[token] = value
        return self


class ReplayTrial(Contract):
    id: Identifier
    strategy_version: str = Field(min_length=1, max_length=80)
    strategy: Literal["follower_signals", "sma_crossover"] = "follower_signals"
    fast_period: int = Field(3, ge=2, le=100)
    slow_period: int = Field(10, ge=3, le=200)
    order_notional: Amount = Decimal(10)
    fee_bps: Bps | None = None
    settlement_fee_bps: Bps | None = None
    fee_version: str | None = Field(None, min_length=1, max_length=80)
    slippage_bps: Bps = Decimal(0)
    decision_latency_ms: int = Field(0, ge=0, le=86_400_000)
    expiry_seconds: int = Field(60, ge=0, le=3600)
    max_book_age_seconds: int = Field(60, ge=1, le=300)
    lot_size: Amount = Decimal("0.01")

    @model_validator(mode="after")
    def parameters(self):
        if self.lot_size > 1 or self.fast_period >= self.slow_period:
            raise ValueError("invalid lot or indicator periods")
        return self


class ReplayDefinition(Contract):
    version: Literal["executable-replay-v1"] = "executable-replay-v1"
    start_at: AwareDatetime
    train_end: AwareDatetime
    validation_end: AwareDatetime
    end_at: AwareDatetime
    fill_policy: Literal["ioc-next-receipt-conservative-depth-v1"] = (
        "ioc-next-receipt-conservative-depth-v1"
    )
    funding: Amount = Decimal(1000)
    sample_seconds: int = Field(86400, ge=1, le=604800)
    annualization_periods: int | None = Field(None, ge=1, le=31_536_000)
    trials: list[ReplayTrial] = Field(min_length=1, max_length=8)
    declared_complete_family: bool = False
    hypothesis: str = Field(min_length=1, max_length=1000)
    tape: ReplayTape

    @model_validator(mode="after")
    def bounds(self):
        if not self.start_at < self.train_end < self.validation_end < self.end_at:
            raise ValueError("chronological train/validation/test windows required")
        if any(
            (boundary - self.start_at) % timedelta(seconds=self.sample_seconds) != timedelta(0)
            for boundary in (self.train_end, self.validation_end, self.end_at)
        ):
            raise ValueError("split boundaries must align with the declared NAV sampling grid")
        if self.end_at - self.start_at > timedelta(days=366):
            raise ValueError("replay window exceeds one year")
        count = int((self.end_at - self.start_at).total_seconds() // self.sample_seconds) + 1
        if count > 2000 or count * max(1, len(self.tape.events)) * len(self.trials) > 250000:
            raise ValueError("replay sampling/work budget exceeded")
        if len({trial.id for trial in self.trials}) != len(self.trials):
            raise ValueError("duplicate trial identity")
        return self
