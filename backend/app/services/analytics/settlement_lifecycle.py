"""Read-only settlement lifecycle, capital-lockup, and redemption risk analysis.

Strictly informational (``execution_eligible=False``); never holds keys, signs
transactions, or bypasses ``pre_trade_gate`` / ``live_trading_policy`` /
``redemption_service`` dry-run guards.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_QUANT_6 = Decimal("0.000001")
_DAYS_PER_YEAR = Decimal("365")

_UNCERTAIN_STATES = frozenset(
    {
        "RESOLUTION_PROPOSED",
        "DISPUTED",
        "CHALLENGED",
        "VOID",
        "REVERSED",
        "TX_PENDING",
        "TX_FAILED",
    }
)
_PRE_SETTLEMENT_STATES = frozenset({"TRADING_OPEN", "TRADING_CLOSED"})


class SettlementState(StrEnum):
    """Lifecycle states from trading through on-chain redemption finality."""

    TRADING_OPEN = "TRADING_OPEN"
    TRADING_CLOSED = "TRADING_CLOSED"
    RESOLUTION_PROPOSED = "RESOLUTION_PROPOSED"
    DISPUTED = "DISPUTED"
    CHALLENGED = "CHALLENGED"
    FINAL = "FINAL"
    VOID = "VOID"
    REVERSED = "REVERSED"
    REDEEMABLE = "REDEEMABLE"
    TX_PENDING = "TX_PENDING"
    TX_CONFIRMED = "TX_CONFIRMED"
    TX_FAILED = "TX_FAILED"


class SettlementInput(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def bounded_values(cls, value):
        if isinstance(value, Decimal) and (
            not value.is_finite()
            or value.copy_abs() > Decimal("1e18")
            or value.as_tuple().exponent < -18
        ):
            raise ValueError(
                "Decimal must be finite, bounded and have at most 18 fractional places"
            )
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Timestamp must include a UTC offset")
        return value


class RedemptionAttemptView(SettlementInput):
    """Read-only snapshot of a dry-run preview or broadcast redemption attempt."""

    attempt_id: str = Field(..., min_length=1)
    dry_run: bool = True
    tx_hash: str | None = None
    chain_receipt_status: Literal["confirmed", "reverted", "pending", "missing"] | None = None
    confirmations: int = Field(default=0, ge=0)
    required_confirmations: int = Field(default=12, ge=1)
    gas_fee_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    payout_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    redeemed_shares: Decimal | None = Field(default=None, ge=Decimal("0"))
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None


class SettlementDiscrepancy(BaseModel):
    """Structured reconciliation discrepancy surfaced during settlement checks."""

    code: str
    severity: Literal["warning", "critical"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SettlementAnalysisRequest(SettlementInput):
    """Input payload for read-only settlement lifecycle & capital lockup analysis."""

    market_id: str = Field(..., min_length=1)
    state: SettlementState
    previous_state: SettlementState | None = None
    position_shares: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    entry_cost_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    expected_payout_per_share: Decimal = Field(
        default=Decimal("1"), ge=Decimal("0"), le=Decimal("1")
    )
    venue_reported_payout_usdc: Decimal | None = Field(default=None, ge=Decimal("0"))
    proxy_token_balance: Decimal | None = Field(default=None, ge=Decimal("0"))
    eoa_token_balance: Decimal | None = Field(default=None, ge=Decimal("0"))
    starting_available_cash_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    locked_collateral_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    annual_funding_rate: Decimal = Field(default=Decimal("0.08"), ge=Decimal("0"))
    expected_lockup_days: Decimal = Field(default=Decimal("2"), ge=Decimal("0"))
    stressed_lockup_days: Decimal = Field(default=Decimal("14"), ge=Decimal("0"))
    dispute_window_ends_at: datetime | None = None
    now: datetime | None = None
    redemption_attempts: list[RedemptionAttemptView] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def stress_order(self):
        if self.stressed_lockup_days < self.expected_lockup_days:
            raise ValueError("Stress duration must be at least expected duration")
        return self


class SettlementLifecycleReport(BaseModel):
    """Advisory settlement lifecycle, lockup cost, and reconciliation report."""

    schema_version: Literal["settlement-lifecycle-v1"] = "settlement-lifecycle-v1"
    provenance_class: Literal["declared_hypothesis"] = "declared_hypothesis"
    source_verified: Literal[False] = False
    input_sha256: str
    execution_eligible: Literal[False] = False
    advisory_only: Literal[True] = True
    market_id: str
    state: SettlementState
    status: Literal["ok", "abstain"]
    abstain_reasons: list[str]
    is_final_settled: bool
    is_redeemable: bool
    has_confirmed_redemption: bool
    has_dry_run_only: bool
    locked_collateral_usdc: Decimal
    released_collateral_usdc: Decimal
    available_cash_usdc: Decimal
    available_cash_after_lockup_usdc: Decimal
    realized_pnl_usdc: Decimal
    unrealized_payout_usdc: Decimal
    total_gas_fee_usdc: Decimal
    expected_opportunity_cost_usdc: Decimal
    stressed_opportunity_cost_usdc: Decimal
    expected_lockup_days: Decimal
    stressed_lockup_days: Decimal
    discrepancies: list[SettlementDiscrepancy]
    operational_risks: list[str]
    analyzed_at: datetime


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _q(val: Decimal) -> Decimal:
    return val.quantize(_QUANT_6, rounding=ROUND_HALF_UP)


def _reconcile_attempts(
    req: SettlementAnalysisRequest,
) -> tuple[list[SettlementDiscrepancy], list[RedemptionAttemptView], list[RedemptionAttemptView]]:
    discrepancies: list[SettlementDiscrepancy] = []
    broadcasts = [a for a in req.redemption_attempts if not a.dry_run]
    confirmed: list[RedemptionAttemptView] = []
    seen_tx_hashes: set[str] = set()

    if len({a.attempt_id for a in req.redemption_attempts}) != len(req.redemption_attempts):
        discrepancies.append(
            SettlementDiscrepancy(
                code="DUPLICATE_ATTEMPT_ID",
                severity="critical",
                message="Attempt identities must be unique.",
            )
        )
    for attempt in req.redemption_attempts:
        if attempt.confirmed_at is not None and (
            attempt.confirmed_at > (req.now or datetime.now(UTC))
            or (attempt.submitted_at is not None and attempt.confirmed_at < attempt.submitted_at)
        ):
            discrepancies.append(
                SettlementDiscrepancy(
                    code="INVALID_RECEIPT_CLOCK",
                    severity="critical",
                    message="Receipt timestamps are inconsistent.",
                )
            )
        if attempt.dry_run and attempt.tx_hash is not None:
            discrepancies.append(
                SettlementDiscrepancy(
                    code="DRY_RUN_WITH_TX_HASH",
                    severity="critical",
                    message=f"Dry-run attempt {attempt.attempt_id} unexpectedly carries a tx_hash.",
                    details={"attempt_id": attempt.attempt_id, "tx_hash": attempt.tx_hash},
                )
            )
        if not attempt.dry_run and not attempt.tx_hash:
            discrepancies.append(
                SettlementDiscrepancy(
                    code="BROADCAST_WITHOUT_TX_HASH",
                    severity="critical",
                    message=f"Broadcast attempt {attempt.attempt_id} is missing tx_hash.",
                    details={"attempt_id": attempt.attempt_id},
                )
            )

    for attempt in broadcasts:
        if attempt.tx_hash:
            if attempt.tx_hash.lower() in seen_tx_hashes:
                discrepancies.append(
                    SettlementDiscrepancy(
                        code="DUPLICATE_TX_HASH",
                        severity="critical",
                        message=f"Duplicate redemption tx_hash detected: {attempt.tx_hash}.",
                        details={"tx_hash": attempt.tx_hash, "attempt_id": attempt.attempt_id},
                    )
                )
            seen_tx_hashes.add(attempt.tx_hash.lower())

        if attempt.chain_receipt_status == "reverted":
            discrepancies.append(
                SettlementDiscrepancy(
                    code="REVERTED_TX",
                    severity="critical",
                    message=(
                        f"Redemption tx {attempt.tx_hash or attempt.attempt_id} reverted on-chain."
                    ),
                    details={"attempt_id": attempt.attempt_id, "tx_hash": attempt.tx_hash},
                )
            )
        elif attempt.tx_hash and attempt.chain_receipt_status in (None, "missing"):
            discrepancies.append(
                SettlementDiscrepancy(
                    code="MISSING_RECEIPT",
                    severity="critical",
                    message=f"Broadcast transaction {attempt.tx_hash} has no on-chain receipt.",
                    details={"attempt_id": attempt.attempt_id, "tx_hash": attempt.tx_hash},
                )
            )
        elif attempt.chain_receipt_status == "confirmed":
            if attempt.confirmations < attempt.required_confirmations:
                discrepancies.append(
                    SettlementDiscrepancy(
                        code="INSUFFICIENT_CONFIRMATIONS",
                        severity="critical",
                        message=(
                            f"Transaction {attempt.tx_hash or attempt.attempt_id} has "
                            f"{attempt.confirmations}/{attempt.required_confirmations} "
                            "confirmations."
                        ),
                        details={
                            "attempt_id": attempt.attempt_id,
                            "confirmations": attempt.confirmations,
                            "required_confirmations": attempt.required_confirmations,
                        },
                    )
                )
            else:
                confirmed.append(attempt)

    for attempt in confirmed:
        quantity = (
            attempt.redeemed_shares if attempt.redeemed_shares is not None else req.position_shares
        )
        if attempt.payout_usdc != quantity * req.expected_payout_per_share:
            discrepancies.append(
                SettlementDiscrepancy(
                    code="REDEEMED_QUANTITY_PAYOUT_MISMATCH",
                    severity="critical",
                    message=(
                        "Confirmed payout disagrees with the declared redeemed quantity and "
                        "payout rule."
                    ),
                )
            )

    if (
        sum(
            (
                a.redeemed_shares if a.redeemed_shares is not None else req.position_shares
                for a in confirmed
            ),
            Decimal(0),
        )
        > req.position_shares
    ):
        discrepancies.append(
            SettlementDiscrepancy(
                code="DUPLICATE_REDEMPTION",
                severity="critical",
                message=(
                    f"Multiple confirmed redemption attempts ({len(confirmed)}) "
                    f"detected for market {req.market_id}."
                ),
                details={"attempt_ids": [a.attempt_id for a in confirmed]},
            )
        )

    return discrepancies, broadcasts, confirmed


def _reconcile_balances_and_state(
    req: SettlementAnalysisRequest,
    confirmed: list[RedemptionAttemptView],
) -> list[SettlementDiscrepancy]:
    discrepancies: list[SettlementDiscrepancy] = []

    if req.state == SettlementState.TX_CONFIRMED and not confirmed:
        discrepancies.append(
            SettlementDiscrepancy(
                code="UNVERIFIED_CONFIRMATION",
                severity="critical",
                message=(
                    "State is TX_CONFIRMED without a finalized broadcast receipt "
                    "meeting confirmation depth."
                ),
            )
        )

    if req.previous_state in {
        SettlementState.FINAL,
        SettlementState.REDEEMABLE,
        SettlementState.TX_CONFIRMED,
    } and req.state in {
        SettlementState.DISPUTED,
        SettlementState.CHALLENGED,
        SettlementState.REVERSED,
        SettlementState.VOID,
    }:
        discrepancies.append(
            SettlementDiscrepancy(
                code="OUTCOME_REVERSAL",
                severity="critical",
                message=(
                    f"Outcome transitioned backward from {req.previous_state.value} "
                    f"to {req.state.value}."
                ),
                details={"previous_state": req.previous_state.value, "state": req.state.value},
            )
        )

    expected_payout = _q(req.position_shares * req.expected_payout_per_share)
    if (
        req.venue_reported_payout_usdc is not None
        and req.state not in _PRE_SETTLEMENT_STATES
        and _q(req.venue_reported_payout_usdc) != expected_payout
    ):
        discrepancies.append(
            SettlementDiscrepancy(
                code="PAYOUT_MISMATCH",
                severity="critical",
                message=(
                    f"Venue reported payout ({_q(req.venue_reported_payout_usdc)}) differs from "
                    f"expected payout ({expected_payout})."
                ),
                details={
                    "venue_reported_payout_usdc": str(_q(req.venue_reported_payout_usdc)),
                    "expected_payout_usdc": str(expected_payout),
                },
            )
        )

    if confirmed:
        onchain_payout = _q(sum((a.payout_usdc for a in confirmed), Decimal(0)))
        if req.venue_reported_payout_usdc is not None and onchain_payout > _q(
            req.venue_reported_payout_usdc
        ):
            discrepancies.append(
                SettlementDiscrepancy(
                    code="PAYOUT_MISMATCH",
                    severity="critical",
                    message=(
                        f"On-chain confirmed payout ({onchain_payout}) differs from venue reported "
                        f"payout ({_q(req.venue_reported_payout_usdc)})."
                    ),
                    details={
                        "onchain_payout_usdc": str(onchain_payout),
                        "venue_reported_payout_usdc": str(_q(req.venue_reported_payout_usdc)),
                    },
                )
            )

    if req.proxy_token_balance is not None or req.eoa_token_balance is not None:
        proxy_bal = req.proxy_token_balance or Decimal("0")
        eoa_bal = req.eoa_token_balance or Decimal("0")
        total_bal = _q(proxy_bal + eoa_bal)
        redeemed = sum(
            (
                a.redeemed_shares if a.redeemed_shares is not None else req.position_shares
                for a in confirmed
            ),
            Decimal(0),
        )
        remaining_shares = max(Decimal(0), req.position_shares - redeemed)
        if total_bal != _q(remaining_shares):
            discrepancies.append(
                SettlementDiscrepancy(
                    code="PROXY_EOA_BALANCE_MISMATCH",
                    severity="critical",
                    message=(
                        f"Proxy ({proxy_bal}) + EOA ({eoa_bal}) token balances do not "
                        f"match remaining unredeemed shares ({remaining_shares})."
                    ),
                    details={
                        "proxy_token_balance": str(proxy_bal),
                        "eoa_token_balance": str(eoa_bal),
                        "position_shares": str(req.position_shares),
                    },
                )
            )

    return discrepancies


def analyze_settlement_lifecycle(req: SettlementAnalysisRequest) -> SettlementLifecycleReport:
    with localcontext(Context(prec=80)):
        return _analyze_settlement_lifecycle(req)


def _analyze_settlement_lifecycle(req: SettlementAnalysisRequest) -> SettlementLifecycleReport:
    """Analyze settlement state, redemption finality, discrepancies, and lockup cost."""
    now = _ensure_utc(req.now) or datetime.now(UTC)
    dispute_ends = _ensure_utc(req.dispute_window_ends_at)

    attempt_discrepancies, broadcasts, confirmed = _reconcile_attempts(req)
    state_discrepancies = _reconcile_balances_and_state(req, confirmed)
    discrepancies = attempt_discrepancies + state_discrepancies
    has_critical_discrepancy = any(d.severity == "critical" for d in discrepancies)

    abstain_reasons: list[str] = []
    if req.state.value in _UNCERTAIN_STATES:
        abstain_reasons.append(f"Settlement state {req.state.value} is not final.")
    if dispute_ends is not None and now < dispute_ends:
        abstain_reasons.append(f"Dispute window remains open until {dispute_ends.isoformat()}.")
    if has_critical_discrepancy:
        abstain_reasons.append("Critical reconciliation discrepancy detected.")

    has_confirmed_redemption = (
        req.state == SettlementState.TX_CONFIRMED and bool(confirmed) and not abstain_reasons
    )
    has_dry_run_only = bool(req.redemption_attempts) and not broadcasts

    is_final_settled = (
        req.state
        in {
            SettlementState.FINAL,
            SettlementState.REDEEMABLE,
            SettlementState.TX_CONFIRMED,
        }
        and (dispute_ends is None or now >= dispute_ends)
        and not has_critical_discrepancy
    )
    is_redeemable = (
        req.state == SettlementState.REDEEMABLE
        and (dispute_ends is None or now >= dispute_ends)
        and not has_critical_discrepancy
    )

    total_gas_fee = _q(sum((a.gas_fee_usdc for a in broadcasts), Decimal("0")))
    expected_payout = _q(req.position_shares * req.expected_payout_per_share)

    confirmed_payout = Decimal(0)
    if has_confirmed_redemption:
        confirmed_payout = _q(sum((a.payout_usdc for a in confirmed), Decimal(0)))
        redeemed = sum(
            (
                a.redeemed_shares if a.redeemed_shares is not None else req.position_shares
                for a in confirmed
            ),
            Decimal(0),
        )
        fraction = redeemed / req.position_shares if req.position_shares else Decimal(0)
        released_collateral = _q(req.locked_collateral_usdc * fraction)
        remaining_locked = _q(req.locked_collateral_usdc - released_collateral)
        realized_pnl = _q(confirmed_payout - req.entry_cost_usdc * fraction - total_gas_fee)
        unrealized_payout = _q((req.position_shares - redeemed) * req.expected_payout_per_share)
    else:
        released_collateral = Decimal("0")
        remaining_locked = _q(req.locked_collateral_usdc)
        realized_pnl = Decimal("0")
        unrealized_payout = (
            expected_payout if req.state != SettlementState.VOID else _q(req.locked_collateral_usdc)
        )

    expected_opp_cost = _q(
        remaining_locked * req.annual_funding_rate * req.expected_lockup_days / _DAYS_PER_YEAR
    )
    stressed_opp_cost = _q(
        remaining_locked * req.annual_funding_rate * req.stressed_lockup_days / _DAYS_PER_YEAR
    )

    available_cash = _q(req.starting_available_cash_usdc + confirmed_payout - total_gas_fee)
    available_cash_after_lockup = _q(available_cash - expected_opp_cost)

    operational_risks: list[str] = []
    if has_dry_run_only:
        operational_risks.append(
            "Only dry-run redemption previews exist; no on-chain transaction broadcast."
        )
    if remaining_locked > 0:
        operational_risks.append(
            f"Collateral ({remaining_locked} USDC) remains locked until "
            "confirmed on-chain finality."
        )
    if req.state in {SettlementState.DISPUTED, SettlementState.CHALLENGED}:
        operational_risks.append(
            "Active oracle dispute/challenge may extend capital lockup and revise payout."
        )
    if req.state == SettlementState.VOID:
        operational_risks.append(
            "Market voided; await confirmed refund transaction before releasing collateral."
        )
    if req.state == SettlementState.REVERSED:
        operational_risks.append("Outcome reversed; prior settlement assumptions are invalidated.")

    status: Literal["ok", "abstain"] = "abstain" if abstain_reasons else "ok"

    return SettlementLifecycleReport(
        input_sha256=hashlib.sha256(req.model_dump_json().encode()).hexdigest(),
        market_id=req.market_id,
        state=req.state,
        status=status,
        abstain_reasons=abstain_reasons,
        is_final_settled=is_final_settled,
        is_redeemable=is_redeemable,
        has_confirmed_redemption=has_confirmed_redemption,
        has_dry_run_only=has_dry_run_only,
        locked_collateral_usdc=remaining_locked,
        released_collateral_usdc=released_collateral,
        available_cash_usdc=available_cash,
        available_cash_after_lockup_usdc=available_cash_after_lockup,
        realized_pnl_usdc=realized_pnl,
        unrealized_payout_usdc=unrealized_payout,
        total_gas_fee_usdc=total_gas_fee,
        expected_opportunity_cost_usdc=expected_opp_cost,
        stressed_opportunity_cost_usdc=stressed_opp_cost,
        expected_lockup_days=req.expected_lockup_days,
        stressed_lockup_days=req.stressed_lockup_days,
        discrepancies=discrepancies,
        operational_risks=operational_risks,
        analyzed_at=now,
    )
