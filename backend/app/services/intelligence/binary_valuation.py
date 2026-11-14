"""Deterministic, Decimal-exact binary contract valuation primitive (#15).

Computes gross/net expected value, break-even probability, conservative
lower-bound edge, sensitivity grid, and bounded Kelly suggestion from a
calibrated probability and size-aware CLOB execution cost (#18), gated by
verified resolution rules (#16).

Strictly read-only/research-only: holds no credentials and cannot sign,
submit, or authorize orders.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

ExactDecimal = Annotated[Decimal, Field(max_digits=36, decimal_places=18)]

SKILL_VERSION = "binary-contract-valuation-v1"
SCHEMA_VERSION = "binary-valuation-v1"


class BinaryValuationInput(BaseModel):
    """Point-in-time inputs for valuing a binary YES/NO contract."""

    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    token_id: str = Field(min_length=1, max_length=120)
    outcome: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"] = "BUY"
    shares: ExactDecimal
    settlement_payout: ExactDecimal = Decimal("1")
    settlement_currency: str = "USDC"
    as_of: datetime
    rules_hash: str
    rule_review_status: Literal["verified", "unverified", "ambiguous", "invalid"]
    calibrated_p_yes: ExactDecimal
    p_yes_lower: ExactDecimal | None = None
    p_yes_upper: ExactDecimal | None = None
    executable_vwap: ExactDecimal
    available_depth_shares: ExactDecimal
    taker_fee_per_share: ExactDecimal
    redemption_cost_per_share: ExactDecimal = Decimal("0")
    adverse_fill_haircut_per_share: ExactDecimal = Decimal("0")
    capital_lockup_days: ExactDecimal = Decimal("0")
    annual_funding_rate: ExactDecimal = Decimal("0")
    fee_schedule_version: str = Field(min_length=1, max_length=80)
    market_implied_p: ExactDecimal | None = None
    forecast_id: int | None = Field(default=None, gt=0)
    account_id: int | None = Field(default=None, gt=0)
    owned_shares: ExactDecimal | None = None
    tick_size: ExactDecimal | None = None
    limit_price: ExactDecimal | None = None
    share_lot: ExactDecimal | None = None
    kelly_fraction_cap: ExactDecimal = Field(
        default=Decimal("0"), ge=Decimal("0"), le=Decimal("0.5")
    )


class ValuationSensitivityPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p_yes: Decimal
    fee_per_share: Decimal
    ev_net_per_share: Decimal
    ev_net_total: Decimal


class ValuationEvidence(BaseModel):
    """Typed, research-only valuation output with explicit abstention reasons."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["binary-valuation-v1"] = SCHEMA_VERSION
    skill_version: str = SKILL_VERSION
    input_hash: str
    condition_id: str
    token_id: str
    outcome: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"]
    as_of: datetime
    rules_hash: str
    fee_schedule_version: str
    status: Literal["valued", "abstain"]
    abstain_reasons: list[str] = Field(default_factory=list)
    ev_gross_per_share: Decimal | None = None
    total_cost_per_share: Decimal | None = None
    ev_net_per_share: Decimal | None = None
    ev_net_total: Decimal | None = None
    ev_net_lower_bound: Decimal | None = None
    break_even_probability: Decimal | None = None
    market_baseline_ev_per_share: Decimal | None = None
    incremental_edge_vs_market: Decimal | None = None
    bounded_kelly_fraction: Decimal | None = None
    sensitivity_grid: list[ValuationSensitivityPoint] = Field(default_factory=list)
    invalidation_triggers: list[str] = Field(default_factory=list)
    decision: Literal["research_only"] = "research_only"
    provenance: dict = Field(default_factory=dict)
    execution_eligible: Literal[False] = False


def _input_digest(inp: BinaryValuationInput) -> str:
    payload = json.loads(inp.model_dump_json())
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _outcome_prob(p_yes: Decimal, outcome: Literal["YES", "NO"]) -> Decimal:
    return p_yes if outcome == "YES" else (Decimal("1") - p_yes)


def _compute_ev(
    *,
    p_yes: Decimal,
    outcome: Literal["YES", "NO"],
    side: Literal["BUY", "SELL"],
    payout: Decimal,
    vwap: Decimal,
    friction_per_share: Decimal,
) -> tuple[Decimal, Decimal]:
    p_out = _outcome_prob(p_yes, outcome)
    ev_gross = p_out * payout - vwap if side == "BUY" else vwap - p_out * payout
    ev_net = ev_gross - friction_per_share
    return ev_gross, ev_net


def value_binary_contract(inp: BinaryValuationInput) -> ValuationEvidence:
    """Conditional cashflow arithmetic; public tooling verifies evidence separately."""
    with localcontext(ACCOUNTING_CONTEXT):
        return _value_binary_contract(inp)


def _value_binary_contract(inp: BinaryValuationInput) -> ValuationEvidence:
    """Deterministically value a binary contract, abstaining on any missing/unverified input."""
    as_of_utc = inp.as_of.astimezone(UTC) if inp.as_of.tzinfo else inp.as_of.replace(tzinfo=UTC)
    input_hash = _input_digest(inp)
    abstain: list[str] = []

    if inp.as_of.tzinfo is None:
        abstain.append("ABSTAIN_NAIVE_TIMESTAMP")
    if inp.rule_review_status != "verified":
        abstain.append("ABSTAIN_UNVERIFIED_OR_AMBIGUOUS_RULE")
    if not inp.rules_hash.strip():
        abstain.append("ABSTAIN_MISSING_RULES_HASH")
    if not inp.shares.is_finite() or inp.shares <= 0:
        abstain.append("ABSTAIN_INVALID_SIZE")
    if not inp.available_depth_shares.is_finite() or inp.available_depth_shares < inp.shares:
        abstain.append("ABSTAIN_INSUFFICIENT_DEPTH")
    if not inp.settlement_payout.is_finite() or inp.settlement_payout <= 0:
        abstain.append("ABSTAIN_INVALID_PAYOUT")
    if not inp.calibrated_p_yes.is_finite() or inp.calibrated_p_yes < 0 or inp.calibrated_p_yes > 1:
        abstain.append("ABSTAIN_INVALID_PROBABILITY")
    if (
        not inp.executable_vwap.is_finite()
        or inp.executable_vwap < 0
        or inp.executable_vwap > inp.settlement_payout
    ):
        abstain.append("ABSTAIN_INVALID_EXECUTABLE_PRICE")
    if any(
        not x.is_finite() or x < 0
        for x in (
            inp.taker_fee_per_share,
            inp.redemption_cost_per_share,
            inp.adverse_fill_haircut_per_share,
            inp.capital_lockup_days,
            inp.annual_funding_rate,
        )
    ):
        abstain.append("ABSTAIN_NEGATIVE_OR_NONFINITE_COST")

    if inp.p_yes_lower is not None and (
        not inp.p_yes_lower.is_finite()
        or inp.p_yes_lower < 0
        or inp.p_yes_lower > inp.calibrated_p_yes
    ):
        abstain.append("ABSTAIN_INVALID_PROBABILITY_INTERVAL")
    if inp.p_yes_upper is not None and (
        not inp.p_yes_upper.is_finite()
        or inp.p_yes_upper > 1
        or inp.p_yes_upper < inp.calibrated_p_yes
    ):
        abstain.append("ABSTAIN_INVALID_PROBABILITY_INTERVAL")

    if inp.p_yes_lower is None or inp.p_yes_upper is None:
        abstain.append("ABSTAIN_MISSING_PROBABILITY_INTERVAL")
    if inp.market_implied_p is not None and (
        not inp.market_implied_p.is_finite() or not 0 <= inp.market_implied_p <= 1
    ):
        abstain.append("ABSTAIN_INVALID_MARKET_BASELINE")
    if inp.side == "SELL" and (
        inp.owned_shares is None
        or not inp.owned_shares.is_finite()
        or inp.owned_shares < inp.shares
    ):
        abstain.append("ABSTAIN_INSUFFICIENT_OWNED_INVENTORY")
    for quantum, value, reason in (
        (inp.tick_size, inp.limit_price, "ABSTAIN_INVALID_TICK"),
        (inp.share_lot, inp.shares, "ABSTAIN_INVALID_SHARE_LOT"),
    ):
        if quantum is not None and (
            not quantum.is_finite()
            or quantum <= 0
            or (value is not None and (not value.is_finite() or value % quantum != 0))
        ):
            abstain.append(reason)

    if abstain:
        return ValuationEvidence(
            input_hash=input_hash,
            condition_id=inp.condition_id.lower(),
            token_id=inp.token_id,
            outcome=inp.outcome,
            side=inp.side,
            as_of=as_of_utc,
            rules_hash=inp.rules_hash,
            fee_schedule_version=inp.fee_schedule_version,
            status="abstain",
            abstain_reasons=list(dict.fromkeys(abstain)),
            invalidation_triggers=[
                f"rule_hash_change:{inp.rules_hash[:12]}",
                "book_staleness_or_depth_drop",
            ],
        )

    lockup_cost = (
        inp.executable_vwap * inp.annual_funding_rate * (inp.capital_lockup_days / Decimal("365"))
    )
    total_friction = (
        inp.taker_fee_per_share
        + inp.redemption_cost_per_share
        + inp.adverse_fill_haircut_per_share
        + lockup_cost
    )

    ev_gross, ev_net = _compute_ev(
        p_yes=inp.calibrated_p_yes,
        outcome=inp.outcome,
        side=inp.side,
        payout=inp.settlement_payout,
        vwap=inp.executable_vwap,
        friction_per_share=total_friction,
    )

    # Conservative lower-bound edge across supplied probability interval.
    p_low = inp.p_yes_lower if inp.p_yes_lower is not None else inp.calibrated_p_yes
    p_high = inp.p_yes_upper if inp.p_yes_upper is not None else inp.calibrated_p_yes
    worst_p_yes = p_low if (inp.outcome == "YES") == (inp.side == "BUY") else p_high
    _, ev_lower = _compute_ev(
        p_yes=worst_p_yes,
        outcome=inp.outcome,
        side=inp.side,
        payout=inp.settlement_payout,
        vwap=inp.executable_vwap,
        friction_per_share=total_friction,
    )

    if inp.side == "BUY":
        break_even_p = (inp.executable_vwap + total_friction) / inp.settlement_payout
    else:
        break_even_p = (inp.executable_vwap - total_friction) / inp.settlement_payout

    market_ev = None
    incremental_edge = None
    if inp.market_implied_p is not None and inp.market_implied_p.is_finite():
        _, market_ev = _compute_ev(
            p_yes=inp.market_implied_p,
            outcome=inp.outcome,
            side=inp.side,
            payout=inp.settlement_payout,
            vwap=inp.executable_vwap,
            friction_per_share=total_friction,
        )
        incremental_edge = ev_net - market_ev

    # No independently approved Kelly parameter exists in the read-side record.
    # A caller-selected cap is not such approval; never turn it into allocation advice.
    kelly = Decimal("0")

    p_grid = list(dict.fromkeys([p_low, inp.calibrated_p_yes, p_high]))
    fee_grid = [inp.taker_fee_per_share, inp.taker_fee_per_share * Decimal("1.5")]
    sensitivity: list[ValuationSensitivityPoint] = []
    for pg in p_grid:
        for fg in fee_grid:
            f_total = total_friction - inp.taker_fee_per_share + fg
            _, ev_g = _compute_ev(
                p_yes=pg,
                outcome=inp.outcome,
                side=inp.side,
                payout=inp.settlement_payout,
                vwap=inp.executable_vwap,
                friction_per_share=f_total,
            )
            sensitivity.append(
                ValuationSensitivityPoint(
                    p_yes=pg,
                    fee_per_share=fg,
                    ev_net_per_share=ev_g,
                    ev_net_total=ev_g * inp.shares,
                )
            )

    return ValuationEvidence(
        input_hash=input_hash,
        condition_id=inp.condition_id.lower(),
        token_id=inp.token_id,
        outcome=inp.outcome,
        side=inp.side,
        as_of=as_of_utc,
        rules_hash=inp.rules_hash,
        fee_schedule_version=inp.fee_schedule_version,
        status="valued",
        ev_gross_per_share=ev_gross,
        total_cost_per_share=total_friction,
        ev_net_per_share=ev_net,
        ev_net_total=ev_net * inp.shares,
        ev_net_lower_bound=ev_lower,
        break_even_probability=break_even_p,
        market_baseline_ev_per_share=market_ev,
        incremental_edge_vs_market=incremental_edge,
        bounded_kelly_fraction=kelly,
        sensitivity_grid=sensitivity,
        invalidation_triggers=[
            f"rule_hash_change:{inp.rules_hash[:12]}",
            "book_staleness_or_depth_drop",
            "calibration_drift_exceeds_threshold",
        ],
    )
