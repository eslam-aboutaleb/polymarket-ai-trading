"""Proof-carrying ArbCandidate: state machine over verified inputs.

Assessment is a pure function of its inputs (books, specs, rules, fees, costs,
an explicit ``as_of``): the same inputs always yield the same candidate and
``input_digest``. Nothing here places, signs or cancels an order.
"""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime

from app.services.analytics.arbitrage import ENGINE_VERSION
from app.services.analytics.arbitrage.arbitrage_solver import (
    CostAssumptions,
    NetBound,
    max_profitable_size,
    solve_basket,
)
from app.services.analytics.arbitrage.depth_aware_pricer import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_MAX_SKEW_SECONDS,
    PricedLeg,
    PricingResult,
    price_legs,
)
from app.services.analytics.arbitrage.inventory_recovery import OneLegExposure, one_leg_exposure
from app.services.analytics.arbitrage.payout_validator import (
    PAYOFF_MATRIX_VERSION,
    ContractRules,
    Equivalence,
    PayoffMatrix,
    build_payoff_matrix,
    validate_binary_complement,
    validate_matrix,
    verify_cross_venue,
)
from app.services.market_data.binary_book import Contract

State = Literal["INDICATIVE", "DEPTH_VERIFIED", "EXECUTION_READY", "STALE", "INVALID", "PARTIAL"]

NON_ATOMIC_NOTICE = (
    "Legs are separate orders and are not atomic. Net bound assumes both legs fill at the "
    "displayed asks; no profit is assured. Paper research only."
)


class LegQuote(Contract):
    label: str
    venue: str
    market_id: str
    token_id: str
    outcome: str
    book_hash: str | None
    book_as_of: AwareDatetime
    age_seconds: Decimal | None
    matched_shares: Decimal
    vwap: Decimal | None
    gross_notional: Decimal | None
    fee: Decimal | None
    net_cash: Decimal | None
    displayed_capacity: Decimal | None
    fee_version: str | None


class ArbCandidate(Contract):
    version: str = ENGINE_VERSION
    payoff_matrix_version: str = PAYOFF_MATRIX_VERSION
    candidate_id: str
    kind: Literal["paired_outcome", "cross_venue"]
    state: State
    venue_ids: list[str]
    condition_ids: list[str]
    as_of: AwareDatetime
    stale_at: AwareDatetime | None
    requested_size: Decimal
    size: Decimal
    max_executable_size: Decimal
    max_profitable_size: Decimal | None
    leg_quotes: list[LegQuote]
    net_bound: NetBound | None
    executable_profit: Decimal | None
    cost_waterfall: list[dict[str, str]]
    payoff_matrix: PayoffMatrix | None
    equivalence: Equivalence | None
    settlement_risk: list[str]
    partial_fill_risk: OneLegExposure | None
    badges: list[str]
    reasons: list[str]
    notices: list[str]
    input_digest: str
    paper_only: bool = True
    live_execution_enabled: bool = False
    profit_assured: bool = False


def canonical_digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _waterfall(bound: NetBound, costs: CostAssumptions, cross_venue: bool) -> list[dict[str, str]]:
    rows = [
        {"item": "worst_case_payoff", "amount": str(bound.worst_case_payoff)},
        {"item": "ask_depth_cost_gross", "amount": str(-bound.gross_notional)},
        {"item": "taker_fees", "amount": str(-bound.total_fees)},
        {"item": "fee_inclusive_bound", "amount": str(bound.fee_inclusive_bound)},
    ]
    for name in costs.required(cross_venue):
        value = getattr(costs, name)
        rows.append({"item": name, "amount": "UNKNOWN" if value is None else str(-value)})
    rows.append({"item": "net_bound", "amount": str(bound.net_bound)})
    return rows


def _build_quotes(
    legs: list[PricedLeg], rules: list[ContractRules], pricing: PricingResult
) -> list[LegQuote]:
    quotes = []
    for leg, rule in zip(legs, rules, strict=True):
        est = pricing.estimates.get(leg.label)
        quotes.append(
            LegQuote(
                label=leg.label,
                venue=rule.venue,
                market_id=rule.market_id,
                token_id=leg.token_id,
                outcome=leg.outcome,
                book_hash=leg.book.book_hash,
                book_as_of=leg.book.as_of,
                age_seconds=leg.book.age_seconds,
                matched_shares=est.matched_shares if est else Decimal(0),
                vwap=est.vwap if est else None,
                gross_notional=est.gross_notional if est else None,
                fee=est.fee if est else None,
                net_cash=est.net_cash if est else None,
                displayed_capacity=est.displayed_capacity if est else None,
                fee_version=leg.fee_version,
            )
        )
    return quotes


def assess_candidate(
    *,
    legs: list[PricedLeg],
    rules: list[ContractRules],
    requested_size: Decimal,
    costs: CostAssumptions,
    as_of: AwareDatetime,
    max_age_seconds: Decimal = DEFAULT_MAX_AGE_SECONDS,
    max_skew_seconds: Decimal = DEFAULT_MAX_SKEW_SECONDS,
) -> ArbCandidate:
    """Assess a two-leg BUY basket: YES+NO of one contract, or one leg per venue."""
    if len(legs) != 2 or len(rules) != 2:
        raise ValueError("TWO_LEGS_REQUIRED")
    cross_venue = rules[0].venue != rules[1].venue or rules[0].market_id != rules[1].market_id
    digest = canonical_digest(
        {
            "engine": ENGINE_VERSION,
            "legs": [leg.model_dump(mode="json") for leg in legs],
            "rules": [rule.model_dump(mode="json") for rule in rules],
            "size": str(requested_size),
            "costs": costs.model_dump(mode="json"),
            "as_of": as_of.isoformat(),
            "max_age": str(max_age_seconds),
            "max_skew": str(max_skew_seconds),
        }
    )
    reasons: list[str] = []
    settlement_risk: list[str] = []
    equivalence: Equivalence | None = None
    structural: list[str] = []
    if cross_venue:
        equivalence = verify_cross_venue(rules[0], rules[1])
        if equivalence.status != "VERIFIED_EQUIVALENT":
            structural.append(f"CROSS_VENUE_{equivalence.status}")
            structural.extend(equivalence.reasons)
        for rule in rules:
            structural.extend(r for r in validate_binary_complement(rule) if r.startswith("PLAY"))
        outcomes = ("YES", "NO")
        if {legs[0].outcome, legs[1].outcome} != set(outcomes):
            structural.append("LEGS_NOT_OPPOSITE_OUTCOMES")
    else:
        structural.extend(validate_binary_complement(rules[0]))
        if {legs[0].outcome, legs[1].outcome} != {"YES", "NO"}:
            structural.append("LEGS_NOT_COMPLEMENTARY_TOKENS")
        if (
            rules[0].yes_token != legs[0].spec.yes_token
            or rules[0].no_token != legs[0].spec.no_token
        ):
            structural.append("RULES_TOKEN_MAPPING_MISMATCH")
        if legs[0].spec.condition_id != legs[1].spec.condition_id:
            structural.append("CONDITION_MISMATCH")
        if legs[0].spec.rule_hash != legs[1].spec.rule_hash:
            structural.append("RULE_HASH_MISMATCH")
    for leg, rule in zip(legs, rules, strict=True):
        if leg.spec.condition_id != rule.market_id and not cross_venue:
            structural.append("RULES_CONDITION_MISMATCH")
        if rule.rule_hash and rule.rule_hash != leg.spec.rule_hash:
            structural.append(f"RULE_HASH_DRIFT:{leg.label}")
        if not leg.spec.active or not leg.spec.supported:
            structural.append(f"MARKET_NOT_ACTIVE_OR_UNSUPPORTED:{leg.label}")
    matrix = build_payoff_matrix(
        [
            (legs[0].label, legs[0].outcome, 0),
            (legs[1].label, legs[1].outcome, 1 if cross_venue else 0),
        ],
        equivalent=not cross_venue
        or (equivalence is not None and equivalence.status == "VERIFIED_EQUIVALENT"),
    )
    structural.extend(validate_matrix(matrix))
    settlement_risk.append("VOID_OR_5050_SCENARIO_MODELED")
    if cross_venue:
        settlement_risk.append("CROSS_VENUE_SETTLEMENT_TIMING_AND_DISPUTE_RISK")
    notices = [NON_ATOMIC_NOTICE]
    badges = ["PAPER_ONLY", "NON_ATOMIC_LEGS"]

    def finish(state: State, **extra) -> ArbCandidate:
        fields = {
            "candidate_id": digest[:16],
            "kind": "cross_venue" if cross_venue else "paired_outcome",
            "state": state,
            "venue_ids": sorted({rule.venue for rule in rules}),
            "condition_ids": sorted({rule.market_id for rule in rules}),
            "as_of": as_of,
            "stale_at": None,
            "requested_size": requested_size,
            "size": Decimal(0),
            "max_executable_size": Decimal(0),
            "max_profitable_size": None,
            "leg_quotes": _build_quotes(
                legs,
                rules,
                PricingResult(status="abstain", reasons=[], requested_size=requested_size),
            ),
            "net_bound": None,
            "executable_profit": None,
            "cost_waterfall": [],
            "payoff_matrix": matrix,
            "equivalence": equivalence,
            "settlement_risk": settlement_risk,
            "partial_fill_risk": None,
            "badges": badges,
            "reasons": sorted(set(reasons)),
            "notices": notices,
            "input_digest": digest,
        }
        fields.update(extra)
        return ArbCandidate(**fields)

    if structural:
        reasons.extend(structural)
        if cross_venue and equivalence is not None and equivalence.status != "MISMATCHED":
            badges.append("REVIEW_ONLY")
        return finish("INVALID")

    pricing = price_legs(
        legs, requested_size, max_age_seconds=max_age_seconds, max_skew_seconds=max_skew_seconds
    )
    if pricing.status != "priced":
        reasons.extend(pricing.reasons)
        if any(r.startswith("UNKNOWN_FEES") for r in pricing.reasons):
            badges.append("UNKNOWN_FEES")
        quotes = _build_quotes(legs, rules, pricing)
        return finish("STALE" if pricing.stale else "INVALID", leg_quotes=quotes)

    bound = solve_basket(pricing, matrix, costs, cross_venue=cross_venue)
    quotes = _build_quotes(legs, rules, pricing)
    stale_at = min(leg.book.as_of for leg in legs) + timedelta(seconds=float(max_age_seconds))
    common: dict[str, Any] = {
        "leg_quotes": quotes,
        "net_bound": bound,
        "size": pricing.common_size,
        "max_executable_size": pricing.max_executable_size,
        "stale_at": stale_at,
        "cost_waterfall": _waterfall(bound, costs, cross_venue),
        "partial_fill_risk": one_leg_exposure(legs, pricing),
    }
    best = max_profitable_size(
        legs,
        pricing,
        matrix,
        costs,
        cross_venue=cross_venue,
        max_age_seconds=max_age_seconds,
        max_skew_seconds=max_skew_seconds,
    )
    common["max_profitable_size"] = best[0] if best else None
    if bound.net_bound <= 0:
        reasons.append("NO_POSITIVE_NET_BOUND")
        if bound.quote_gross_gap > 0:
            reasons.append("QUOTE_GAP_ERASED_BY_COSTS")
        return finish("INVALID", **common)
    if bound.unknown_costs:
        badges.append("UNKNOWN_FEES")
        reasons.extend(f"UNKNOWN_COST:{name}" for name in bound.unknown_costs)
        return finish("DEPTH_VERIFIED", **common)
    common["executable_profit"] = bound.net_bound
    if pricing.partial:
        reasons.append("SIZE_SCALED_TO_COMMON_DEPTH")
        return finish("PARTIAL", **common)
    return finish("EXECUTION_READY", **common)


def indicative_candidate(
    *,
    kind: Literal["paired_outcome", "cross_venue"],
    venue_ids: list[str],
    condition_ids: list[str],
    gross_gap: Decimal,
    as_of: AwareDatetime,
    detail: dict[str, Any],
) -> ArbCandidate:
    """Scanner-level INDICATIVE record: a price gap only, never executable."""
    digest = canonical_digest(
        {
            "engine": ENGINE_VERSION,
            "kind": kind,
            "venues": venue_ids,
            "conditions": condition_ids,
            "gap": str(gross_gap),
            "as_of": as_of.isoformat(),
            "detail": detail,
        }
    )
    return ArbCandidate(
        candidate_id=digest[:16],
        kind=kind,
        state="INDICATIVE",
        venue_ids=venue_ids,
        condition_ids=condition_ids,
        as_of=as_of,
        stale_at=None,
        requested_size=Decimal(0),
        size=Decimal(0),
        max_executable_size=Decimal(0),
        max_profitable_size=None,
        leg_quotes=[],
        net_bound=None,
        executable_profit=None,
        cost_waterfall=[],
        payoff_matrix=None,
        equivalence=None,
        settlement_risk=[],
        partial_fill_risk=None,
        badges=["INDICATIVE_ONLY", "NOT_EXECUTABLE", "UNKNOWN_FEES"],
        reasons=["DEPTH_NOT_VERIFIED", "FEES_UNKNOWN", "SETTLEMENT_RULES_NOT_VERIFIED"],
        notices=[NON_ATOMIC_NOTICE],
        input_digest=digest,
    )
