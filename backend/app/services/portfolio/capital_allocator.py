"""Scenario-aware, advisory-only capital allocator (issue #10).

Sizing is bounded by (a) a calibrated lower-bound probability, (b) a fractional Kelly cap,
(c) displayed depth, (d) cash never double-allocated (existing reservations count),
(e) worst-case loss over *feasible* joint states from the event-correlation engine
(declared logical relations exclude impossible states), and (f) cluster concentration.
Output is a proposal: ``execution_eligible`` is always False and nothing here can place,
sign or cancel an order, or change a risk limit.
"""

from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.services.analytics.event_correlation_risk import (
    EventCorrelationRequest,
    ExposureLeg,
    LogicalRelation,
    assess_event_correlation_risk,
)
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

ALLOCATOR_VERSION = "scenario-allocator-v1"
Money = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000"))]
Prob = Annotated[Decimal, Field(ge=0, le=1)]


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=120)
    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    token_id: str = Field(min_length=1, max_length=120)
    outcome: Literal["YES", "NO"]
    ask_price: Annotated[Decimal, Field(gt=0, lt=1)]
    fee_per_share: Money = Decimal(0)
    depth_shares: Money
    # Out-of-sample calibrated probability that THIS outcome wins. None = no forecast.
    p_calibrated: Prob | None = None
    p_uncertainty: Prob = Decimal("0.05")  # half-width subtracted before sizing
    calibration_oos_n: int = Field(default=0, ge=0)
    event_id: str | None = Field(default=None, max_length=100)


class AllocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime
    account_cash: Money
    reserved_cash: Money = Decimal(0)  # resting-order reservations, never re-spent
    existing_legs: list[ExposureLeg] = Field(default_factory=list, max_length=200)
    relations: list[LogicalRelation] = Field(default_factory=list, max_length=200)
    candidates: list[Candidate] = Field(min_length=1, max_length=50)
    max_worst_case_loss: Money
    kelly_cap: Annotated[Decimal, Field(gt=0, le=Decimal("0.25"))] = Decimal("0.25")
    max_cluster_concentration_pct: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.40")
    min_calibration_oos_n: int = Field(default=30, ge=1)
    # Frozen out-of-sample resolutions (candidate_id -> did its outcome win) for the comparison.
    oos_outcomes: dict[str, bool] | None = None


class Sizing(BaseModel):
    candidate_id: str
    shares: Decimal
    cost: Decimal
    kelly_fraction_used: Decimal
    marginal_worst_loss: Decimal


class Rejection(BaseModel):
    candidate_id: str
    reasons: list[str]


class PolicyResult(BaseModel):
    name: str
    capital_required: Decimal
    worst_case_loss: Decimal
    expected_return_lower_bound: Decimal | None
    oos_realized_pnl: Decimal | None
    oos_note: str


class AllocationProposal(BaseModel):
    allocator_version: str = ALLOCATOR_VERSION
    sizes: list[Sizing]
    rejected: list[Rejection]
    capital_required: Decimal
    cash_available_before: Decimal
    cash_remaining: Decimal
    worst_case_loss: Decimal
    max_worst_case_loss: Decimal
    cluster_warnings: list[str]
    comparison: list[PolicyResult]
    decision: Literal["advisory_only"] = "advisory_only"
    execution_eligible: Literal[False] = False
    as_of: AwareDatetime


def _leg(c: Candidate, shares: Decimal) -> ExposureLeg:
    return ExposureLeg(
        condition_id=c.condition_id,
        token_id=c.token_id,
        outcome=c.outcome,
        shares=shares,
        entry_price=c.ask_price,
        fee_per_share=c.fee_per_share,
        status="open_position",
        leg_id=c.candidate_id,
    )


def _assess(req: AllocationRequest, legs: list[ExposureLeg]):
    return assess_event_correlation_risk(
        EventCorrelationRequest(
            legs=legs,
            relations=req.relations,
            max_cluster_concentration_pct=req.max_cluster_concentration_pct,
            as_of=req.as_of,
        )
    )


def _lower_prob(c: Candidate) -> Decimal | None:
    if c.p_calibrated is None:
        return None
    return max(Decimal(0), c.p_calibrated - c.p_uncertainty)


def _oos(c: Candidate, shares: Decimal, outcomes: dict[str, bool] | None) -> Decimal | None:
    if outcomes is None or c.candidate_id not in outcomes:
        return None
    unit = (Decimal(1) if outcomes[c.candidate_id] else Decimal(0)) - c.ask_price - c.fee_per_share
    return shares * unit


def allocate(req: AllocationRequest) -> AllocationProposal:
    with localcontext(ACCOUNTING_CONTEXT):
        return _allocate(req)


def _allocate(req: AllocationRequest) -> AllocationProposal:
    cash0 = max(Decimal(0), req.account_cash - req.reserved_cash)
    cash = cash0
    legs = list(req.existing_legs)
    base = _assess(req, legs)
    if base.status != "assessed":
        raise ValueError(f"existing exposure not assessable: {base.abstain_reasons}")
    current_loss = base.worst_case_portfolio_loss or Decimal(0)
    sizes: list[Sizing] = []
    rejected: list[Rejection] = []
    warnings: list[str] = list(base.concentration_warnings)

    ranked = sorted(
        req.candidates,
        key=lambda c: (
            (_lower_prob(c) or Decimal(0)) - c.ask_price - c.fee_per_share,
            c.candidate_id,
        ),
        reverse=True,
    )
    for c in ranked:
        why: list[str] = []
        p_lb = _lower_prob(c)
        cost_unit = c.ask_price + c.fee_per_share
        if p_lb is None:
            why.append("MISSING_CALIBRATED_FORECAST")
        elif c.calibration_oos_n < req.min_calibration_oos_n:
            why.append("INSUFFICIENT_OOS_CALIBRATION")
        elif p_lb <= cost_unit:
            why.append("NO_POSITIVE_EDGE_AFTER_UNCERTAINTY")
        if why:
            rejected.append(Rejection(candidate_id=c.candidate_id, reasons=why))
            continue
        assert p_lb is not None
        frac = min(req.kelly_cap, (p_lb - cost_unit) / (1 - cost_unit))
        shares = min(c.depth_shares, (cash * frac) / cost_unit)
        shares = shares.quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        marginal = Decimal(0)
        while shares > 0:
            trial = _assess(req, [*legs, _leg(c, shares)])
            loss = trial.worst_case_portfolio_loss or Decimal(0)
            over_cluster = any(
                cl.concentration_pct > req.max_cluster_concentration_pct for cl in trial.clusters
            ) and not any(
                cl.concentration_pct > req.max_cluster_concentration_pct for cl in base.clusters
            )
            if loss <= req.max_worst_case_loss and not over_cluster and shares * cost_unit <= cash:
                marginal = loss - current_loss
                current_loss = loss
                break
            shares = (shares / 2).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        if shares <= 0:
            rejected.append(Rejection(candidate_id=c.candidate_id, reasons=["RISK_LIMIT_OR_CASH"]))
            continue
        cost = shares * cost_unit
        cash -= cost
        legs.append(_leg(c, shares))
        sizes.append(
            Sizing(
                candidate_id=c.candidate_id,
                shares=shares,
                cost=cost,
                kelly_fraction_used=frac,
                marginal_worst_loss=marginal,
            )
        )
    final = _assess(req, legs)
    capital = sum((s.cost for s in sizes), Decimal(0))
    by_id = {c.candidate_id: c for c in req.candidates}
    comparison = [
        _policy("scenario_allocator", sizes, by_id, req, final.worst_case_portfolio_loss),
        _equal_risk(
            req,
            by_id,
            [c for c in ranked if c.candidate_id in {s.candidate_id for s in sizes}] or ranked,
            cash0,
        ),
        PolicyResult(
            name="no_trade",
            capital_required=Decimal(0),
            worst_case_loss=Decimal(0),
            expected_return_lower_bound=Decimal(0),
            oos_realized_pnl=Decimal(0) if req.oos_outcomes is not None else None,
            oos_note="baseline",
        ),
    ]
    warnings += list(final.concentration_warnings)
    return AllocationProposal(
        sizes=sizes,
        rejected=rejected,
        capital_required=capital,
        cash_available_before=cash0,
        cash_remaining=cash,
        worst_case_loss=final.worst_case_portfolio_loss or Decimal(0),
        max_worst_case_loss=req.max_worst_case_loss,
        cluster_warnings=sorted(set(warnings)),
        comparison=comparison,
        as_of=req.as_of,
    )


def _policy(name, sizes, by_id, req, worst) -> PolicyResult:
    elb = Decimal(0)
    pnl: Decimal | None = Decimal(0)
    for s in sizes:
        c = by_id[s.candidate_id]
        p = _lower_prob(c) or Decimal(0)
        elb += s.shares * (p - c.ask_price - c.fee_per_share)
        o = _oos(c, s.shares, req.oos_outcomes)
        pnl = None if (pnl is None or o is None) else pnl + o
    return PolicyResult(
        name=name,
        capital_required=sum((s.cost for s in sizes), Decimal(0)),
        worst_case_loss=worst or Decimal(0),
        expected_return_lower_bound=elb,
        oos_realized_pnl=pnl,
        oos_note="frozen outcomes supplied by caller"
        if pnl is not None
        else "no frozen out-of-sample outcomes supplied for every position",
    )


def _equal_risk(req, by_id, cands, cash0) -> PolicyResult:
    """Same candidates, equal cash split, no edge or uncertainty used; still cash-bounded."""
    sizes: list[Sizing] = []
    each = cash0 / len(cands)
    for c in cands:
        unit = c.ask_price + c.fee_per_share
        shares = min(c.depth_shares, each / unit).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
        if shares > 0:
            sizes.append(
                Sizing(
                    candidate_id=c.candidate_id,
                    shares=shares,
                    cost=shares * unit,
                    kelly_fraction_used=Decimal(0),
                    marginal_worst_loss=Decimal(0),
                )
            )
    legs = [*req.existing_legs, *[_leg(by_id[s.candidate_id], s.shares) for s in sizes]]
    worst = _assess(req, legs).worst_case_portfolio_loss
    return _policy("equal_risk_equal_cash", sizes, by_id, req, worst)
