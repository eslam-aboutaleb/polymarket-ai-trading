"""Deterministic event-correlation and scenario portfolio risk (#21).

Detects hidden event/underlying/oracle concentration across settled positions,
open positions, and pending orders using feasible joint-state scenario P&L
and conservative bounds when logical relations are missing.

Strictly read-only/advisory: never overrides ``pre_trade_gate.py`` or
``live_trading_policy.py``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

ExactDecimal = Annotated[Decimal, Field(max_digits=36, decimal_places=18)]

SKILL_VERSION = "event-correlation-risk-v1"
SCHEMA_VERSION = "event-correlation-risk-v1"


class ExposureLeg(BaseModel):
    """Single position, pending order, or unsettled payout leg."""

    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    token_id: str = Field(min_length=1, max_length=120)
    outcome: Literal["YES", "NO"]
    shares: ExactDecimal
    entry_price: ExactDecimal
    fee_per_share: ExactDecimal = Decimal(0)
    status: Literal["open_position", "pending_order", "unsettled_payout"] = "open_position"
    leg_id: str | None = Field(default=None, min_length=1, max_length=200)
    filled_shares: ExactDecimal | None = None
    event_id: str | None = Field(default=None, max_length=100)
    underlying_asset: str | None = Field(default=None, max_length=40)
    oracle: str | None = Field(default=None, max_length=100)
    market_family: str | None = Field(default=None, max_length=100)
    jurisdiction: str | None = Field(default=None, max_length=100)
    resolution_utc: datetime | None = None
    rule_hash: str | None = None


class LogicalRelation(BaseModel):
    """Declared logical relationship between two conditions."""

    model_config = ConfigDict(extra="forbid")

    relation_type: Literal["identical", "mutually_exclusive", "implies", "complementary_hedge"]
    left_condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    right_condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")


class EventClusterExposure(BaseModel):
    """Aggregated exposure and worst-case loss for a correlated cluster."""

    model_config = ConfigDict(extra="forbid")

    cluster_key: str
    cluster_basis: Literal[
        "event_id",
        "underlying_asset",
        "oracle",
        "condition_id",
        "market_family",
        "jurisdiction",
        "resolution_window",
    ]
    condition_ids: list[str]
    gross_notional: Decimal
    pending_notional: Decimal
    worst_case_loss: Decimal
    net_hedged_offset: Decimal
    concentration_pct: Decimal


class EventCorrelationRequest(BaseModel):
    """Input payload for assessing portfolio event-correlation risk."""

    model_config = ConfigDict(extra="forbid")

    legs: list[ExposureLeg] = Field(default_factory=list, max_length=500)
    relations: list[LogicalRelation] = Field(default_factory=list, max_length=500)
    proposed_leg: ExposureLeg | None = None
    max_cluster_concentration_pct: Decimal = Field(
        default=Decimal("0.40"), gt=Decimal("0"), le=Decimal("1")
    )
    as_of: datetime | None = None


class EventRiskAssessment(BaseModel):
    """Read-only advisory event-correlation risk assessment."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event-correlation-risk-v1"] = SCHEMA_VERSION
    skill_version: str = SKILL_VERSION
    status: Literal["assessed", "abstain"]
    abstain_reasons: list[str] = Field(default_factory=list)
    total_gross_notional: Decimal | None = None
    total_pending_notional: Decimal | None = None
    worst_case_portfolio_loss: Decimal | None = None
    cash_tied_until_settlement: Decimal | None = None
    stress_loss_upper_bound: Decimal | None = None
    expected_shortfall: Decimal | None = None
    uncertainty_reason: str = "No independently supported joint outcome probability distribution"
    source_metadata: dict = Field(default_factory=dict)
    proposed_marginal_worst_loss: Decimal | None = None
    clusters: list[EventClusterExposure] = Field(default_factory=list)
    concentration_warnings: list[str] = Field(default_factory=list)
    invalidation_triggers: list[str] = Field(default_factory=list)
    decision: Literal["advisory_only"] = "advisory_only"
    input_hash: str = ""
    as_of: datetime | None = None
    scenario_method: str = "exact_declared_states_up_to_12_conditions_otherwise_conservative_bound"
    evidence_class: str = "caller_declared_scenario_not_reconciled_account_state"
    execution_eligible: Literal[False] = False


def _leg_notional(leg: ExposureLeg) -> Decimal:
    return leg.shares * (leg.entry_price + leg.fee_per_share)


def _leg_pnl(leg: ExposureLeg, yes_wins: bool) -> Decimal:
    won = (leg.outcome == "YES") == yes_wins
    unit_pnl = (Decimal(1) if won else Decimal(0)) - leg.entry_price - leg.fee_per_share
    if leg.status != "pending_order":
        return leg.shares * unit_pnl
    filled = leg.filled_shares or Decimal(0)
    # Filled shares are exposed. The remaining BUY can fill or never fill;
    # its profitable branch cannot be credited as a guaranteed hedge.
    return filled * unit_pnl + (leg.shares - filled) * min(Decimal(0), unit_pnl)


def _condition_worst_loss(legs: list[ExposureLeg]) -> tuple[Decimal, Decimal]:
    gross = sum((_leg_notional(leg) for leg in legs), Decimal(0))
    worst = max(
        Decimal(0),
        -min(sum((_leg_pnl(leg, state) for leg in legs), Decimal(0)) for state in (False, True)),
    )
    return worst, max(Decimal(0), gross - worst)


def _feasible(state, relations):
    for relation in relations:
        left = state.get(relation.left_condition_id.lower())
        right = state.get(relation.right_condition_id.lower())
        if left is None or right is None:
            continue
        if (
            (relation.relation_type == "identical" and left != right)
            or (relation.relation_type == "complementary_hedge" and left == right)
            or (relation.relation_type == "mutually_exclusive" and left and right)
            or (relation.relation_type == "implies" and left and not right)
        ):
            return False
    return True


def _relations_consistent(cids, relations):
    """All supported relations are 2-CNF; check consistency without enumeration."""
    nodes = [(cid, state) for cid in cids for state in (False, True)]
    edges, reverse = {node: set() for node in nodes}, {node: set() for node in nodes}

    def link(a, b):
        edges[a].add(b)
        reverse[b].add(a)

    for relation in relations:
        a, b = relation.left_condition_id.lower(), relation.right_condition_id.lower()
        if relation.relation_type == "identical":
            for state in (False, True):
                link((a, state), (b, state))
                link((b, state), (a, state))
        elif relation.relation_type == "complementary_hedge":
            for state in (False, True):
                link((a, state), (b, not state))
                link((b, state), (a, not state))
        elif relation.relation_type == "mutually_exclusive":
            link((a, True), (b, False))
            link((b, True), (a, False))
        else:  # A implies B: (not A or B).
            link((a, True), (b, True))
            link((b, False), (a, False))
    seen, finished = set(), []
    for node in nodes:
        stack = [(node, False)]
        while stack:
            current, exit_node = stack.pop()
            if exit_node:
                finished.append(current)
            elif current not in seen:
                seen.add(current)
                stack.append((current, True))
                stack.extend((other, False) for other in edges[current] if other not in seen)
    components = {}
    for node in reversed(finished):
        if node in components:
            continue
        component = len(components)
        stack = [node]
        while stack:
            current = stack.pop()
            if current in components:
                continue
            components[current] = component
            stack.extend(other for other in reverse[current] if other not in components)
    return all(components[(cid, False)] != components[(cid, True)] for cid in cids)


def _cluster_worst_loss(cond_legs, relations):
    """Enumerate feasible settlement states with independent pending-fill branches.

    Large portfolios use the sum of per-condition worst losses, a conservative
    upper bound. Declared relations are assumptions, never verified facts or
    evidence of observed diversification. All-NO remains feasible for mutex.
    """
    gross = sum((_leg_notional(leg) for legs in cond_legs.values() for leg in legs), Decimal(0))
    if len(cond_legs) > 12:
        worst = sum((_condition_worst_loss(legs)[0] for legs in cond_legs.values()), Decimal(0))
        return worst, max(Decimal(0), gross - worst)
    cids = sorted(cond_legs)
    pnl = {
        cid: [
            sum((_leg_pnl(leg, outcome) for leg in cond_legs[cid]), Decimal(0))
            for outcome in (False, True)
        ]
        for cid in cids
    }
    losses = []
    for values in itertools.product((False, True), repeat=len(cids)):
        state = dict(zip(cids, values, strict=True))
        if _feasible(state, relations):
            losses.append(-sum((pnl[cid][int(state[cid])] for cid in cids), Decimal(0)))
    if not losses:
        raise ValueError("ABSTAIN_CONTRADICTORY_RELATIONS")
    worst = max(Decimal(0), max(losses))
    return worst, max(Decimal(0), gross - worst)


def _cluster_identities(leg):
    # Exposure groups overlap: an event tag must not hide BTC/oracle exposure.
    groups = [(f"condition:{leg.condition_id.lower()}", "condition_id")]
    if leg.event_id and leg.event_id.strip():
        groups.append((f"event:{leg.event_id.strip().lower()}", "event_id"))
    if leg.underlying_asset and leg.underlying_asset.strip():
        groups.append((f"asset:{leg.underlying_asset.strip().upper()}", "underlying_asset"))
    if leg.oracle and leg.oracle.strip():
        groups.append((f"oracle:{leg.oracle.strip().lower()}", "oracle"))
    for field in ("market_family", "jurisdiction"):
        value = getattr(leg, field)
        if value and value.strip():
            groups.append((f"{field}:{value.strip().casefold()}", field))
    if leg.resolution_utc is not None:
        groups.append(
            (f"time:{leg.resolution_utc.astimezone(UTC).isoformat()}", "resolution_window")
        )
    return groups


def _evaluate_portfolio(
    legs: list[ExposureLeg],
    relations: list[LogicalRelation],
    max_cluster_pct: Decimal,
) -> tuple[Decimal, Decimal, Decimal, list[EventClusterExposure], list[str]]:
    total_gross = sum((_leg_notional(leg) for leg in legs), Decimal("0"))
    total_pending = sum(
        (
            (leg.shares - (leg.filled_shares or Decimal(0))) * (leg.entry_price + leg.fee_per_share)
            for leg in legs
            if leg.status == "pending_order"
        ),
        Decimal("0"),
    )
    if not legs or total_gross <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0"), [], []

    grouped: dict[
        str,
        tuple[
            Literal[
                "event_id",
                "underlying_asset",
                "oracle",
                "condition_id",
                "market_family",
                "jurisdiction",
                "resolution_window",
            ],
            dict[str, list[ExposureLeg]],
        ],
    ] = {}
    for leg in legs:
        for key, basis in _cluster_identities(leg):
            if key not in grouped:
                grouped[key] = (basis, {})
            grouped[key][1].setdefault(leg.condition_id.lower(), []).append(leg)

    clusters: list[EventClusterExposure] = []
    warnings: list[str] = []
    conditions = {}
    for leg in legs:
        conditions.setdefault(leg.condition_id.lower(), []).append(leg)
    total_worst, _ = _cluster_worst_loss(conditions, relations)

    for key, (basis, cond_map) in sorted(grouped.items()):
        cluster_legs = [leg for ls in cond_map.values() for leg in ls]
        gross = sum((_leg_notional(leg) for leg in cluster_legs), Decimal("0"))
        pending = sum(
            (
                (leg.shares - (leg.filled_shares or Decimal(0)))
                * (leg.entry_price + leg.fee_per_share)
                for leg in cluster_legs
                if leg.status == "pending_order"
            ),
            Decimal("0"),
        )
        worst_loss, hedged_offset = _cluster_worst_loss(cond_map, relations)
        ratio = gross / total_gross
        conc = ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        clusters.append(
            EventClusterExposure(
                cluster_key=key,
                cluster_basis=basis,
                condition_ids=sorted(cond_map.keys()),
                gross_notional=gross,
                pending_notional=pending,
                worst_case_loss=worst_loss,
                net_hedged_offset=hedged_offset,
                concentration_pct=conc,
            )
        )
        if ratio > max_cluster_pct:
            warnings.append(f"CLUSTER_CONCENTRATION_EXCEEDED:{key}")
        if len(cond_map) > 1:
            warnings.append(f"AGGREGATED_CORRELATED_CONTRACTS:{key}")

    return total_gross, total_pending, total_worst, clusters, list(dict.fromkeys(warnings))


def assess_event_correlation_risk(req: EventCorrelationRequest) -> EventRiskAssessment:
    with localcontext(ACCOUNTING_CONTEXT):
        return _assess_event_correlation_risk(req)


def _assess_event_correlation_risk(req: EventCorrelationRequest) -> EventRiskAssessment:
    """Compute scenario-based event-correlation risk and marginal proposed-leg impact."""
    as_of = req.as_of.astimezone(UTC) if req.as_of and req.as_of.tzinfo else None
    input_hash = hashlib.sha256(
        json.dumps(
            json.loads(req.model_dump_json()), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if req.as_of is not None and req.as_of.tzinfo is None:
        return EventRiskAssessment(
            status="abstain",
            abstain_reasons=["ABSTAIN_NAIVE_TIMESTAMP"],
            input_hash=input_hash,
            as_of=as_of,
        )

    all_legs = list(req.legs) + ([req.proposed_leg] if req.proposed_leg else [])
    identities = [leg.leg_id for leg in all_legs if leg.leg_id is not None]
    if len(set(identities)) != len(identities):
        return EventRiskAssessment(
            status="abstain",
            abstain_reasons=["ABSTAIN_DUPLICATE_LEG_ID"],
            input_hash=input_hash,
            as_of=as_of,
        )
    present = {leg.condition_id.lower() for leg in all_legs}
    if any(
        r.left_condition_id.lower() not in present or r.right_condition_id.lower() not in present
        for r in req.relations
    ):
        return EventRiskAssessment(
            status="abstain",
            abstain_reasons=["ABSTAIN_UNKNOWN_RELATION_CONDITION"],
            input_hash=input_hash,
            as_of=as_of,
        )
    token_map, outcome_map = {}, {}
    for leg in all_legs:
        outcome_key = (leg.condition_id.lower(), leg.outcome)
        if (
            leg.token_id in token_map
            and token_map[leg.token_id] != (leg.condition_id.lower(), leg.outcome)
        ) or (outcome_key in outcome_map and outcome_map[outcome_key] != leg.token_id):
            return EventRiskAssessment(
                status="abstain",
                abstain_reasons=["ABSTAIN_CONTRADICTORY_TOKEN_MAPPING"],
                input_hash=input_hash,
                as_of=as_of,
            )
        token_map[leg.token_id] = (leg.condition_id.lower(), leg.outcome)
        outcome_map[outcome_key] = leg.token_id
        if (
            not leg.shares.is_finite()
            or leg.shares <= 0
            or not leg.entry_price.is_finite()
            or leg.entry_price < 0
            or leg.entry_price > 1
            or not leg.fee_per_share.is_finite()
            or leg.fee_per_share < 0
            or (
                leg.filled_shares is not None
                and (not leg.filled_shares.is_finite() or not 0 <= leg.filled_shares <= leg.shares)
            )
        ):
            return EventRiskAssessment(
                status="abstain",
                abstain_reasons=["ABSTAIN_INVALID_EXPOSURE_LEG"],
                input_hash=input_hash,
                as_of=as_of,
            )
        if leg.resolution_utc is not None and leg.resolution_utc.tzinfo is None:
            return EventRiskAssessment(
                status="abstain",
                abstain_reasons=["ABSTAIN_NAIVE_RESOLUTION_TIMESTAMP"],
                input_hash=input_hash,
                as_of=as_of,
            )

    if not _relations_consistent(present, req.relations):
        return EventRiskAssessment(
            status="abstain",
            input_hash=input_hash,
            as_of=as_of,
            abstain_reasons=["ABSTAIN_CONTRADICTORY_RELATIONS"],
        )
    if not all_legs:
        return EventRiskAssessment(
            status="assessed",
            input_hash=input_hash,
            as_of=as_of,
            total_gross_notional=Decimal(0),
            total_pending_notional=Decimal(0),
            worst_case_portfolio_loss=Decimal(0),
            cash_tied_until_settlement=Decimal(0),
            stress_loss_upper_bound=Decimal(0),
            invalidation_triggers=["new_position_or_pending_order"],
        )

    try:
        _, _, base_worst, _, _ = _evaluate_portfolio(
            req.legs, req.relations, req.max_cluster_concentration_pct
        )
        gross, pending, worst, clusters, warnings = _evaluate_portfolio(
            all_legs, req.relations, req.max_cluster_concentration_pct
        )
    except ValueError as exc:
        return EventRiskAssessment(
            status="abstain", abstain_reasons=[str(exc)], input_hash=input_hash, as_of=as_of
        )
    marginal = (worst - base_worst) if req.proposed_leg is not None else None

    return EventRiskAssessment(
        status="assessed",
        input_hash=input_hash,
        as_of=as_of,
        scenario_method="exact_declared_feasible_states"
        if len(present) <= 12
        else "sum_condition_worst_loss_bound",
        total_gross_notional=gross,
        total_pending_notional=pending,
        worst_case_portfolio_loss=worst,
        cash_tied_until_settlement=gross,
        stress_loss_upper_bound=gross,
        proposed_marginal_worst_loss=marginal,
        clusters=clusters,
        concentration_warnings=warnings,
        invalidation_triggers=[
            "rule_hash_or_oracle_change",
            "pending_order_fill_or_cancel",
        ],
    )
