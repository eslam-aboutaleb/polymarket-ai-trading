"""Contract equivalence and payoff-scenario matrices for arbitrage legs.

A price gap only becomes a payoff bound after (a) the two legs are proven to
settle on the same event and (b) every legal resolution scenario is enumerated.
Anything not proven stays ``REVIEW_ONLY``; title or fuzzy overlap is never
promoted to economic parity.
"""

from collections.abc import Sequence
from decimal import Decimal, localcontext
from itertools import product
from typing import Literal

from pydantic import AwareDatetime, Field

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import Contract

PAYOFF_MATRIX_VERSION = "arb-payoff-matrix-v1"

# Resolution states of ONE venue's binary contract. VOID covers a venue-level
# 50/50 or cancelled resolution, the state a naive YES+NO model forgets.
STATES = ("YES", "NO", "VOID")
_PAYOUT = {
    ("YES", "YES"): Decimal(1),
    ("YES", "NO"): Decimal(0),
    ("NO", "YES"): Decimal(0),
    ("NO", "NO"): Decimal(1),
    ("YES", "VOID"): Decimal("0.5"),
    ("NO", "VOID"): Decimal("0.5"),
}


class ContractRules(Contract):
    """Verified resolution facts for one binary contract on one venue."""

    venue: str = Field(min_length=1, max_length=40)
    market_id: str = Field(min_length=1, max_length=200)
    yes_token: str | None = Field(default=None, min_length=1, max_length=120)
    no_token: str | None = Field(default=None, min_length=1, max_length=120)
    rule_hash: str | None = Field(default=None, max_length=64)
    event_key: str | None = Field(default=None, max_length=200)
    oracle: str | None = Field(default=None, max_length=200)
    cutoff: AwareDatetime | None = None
    yes_means: str | None = Field(default=None, max_length=200)
    currency: str | None = Field(default=None, max_length=16)
    settlement: str | None = Field(default=None, max_length=200)
    collateral: str | None = Field(default=None, max_length=64)
    neg_risk: bool | None = None
    is_real_money: bool = True
    rules_text: str | None = Field(default=None, max_length=4000)


class Equivalence(Contract):
    status: Literal["VERIFIED_EQUIVALENT", "REVIEW_ONLY", "MISMATCHED", "NON_EXECUTABLE"]
    reasons: list[str]
    checked: list[str]


_CROSS_FIELDS = ("event_key", "oracle", "cutoff", "yes_means", "currency", "settlement")


def validate_binary_complement(rules: ContractRules) -> list[str]:
    """Reasons the YES/NO token pair of one contract is NOT a valid complement."""
    reasons: list[str] = []
    if rules.yes_token is None or rules.no_token is None:
        reasons.append("TOKEN_MAPPING_UNKNOWN")
    elif rules.yes_token == rules.no_token:
        reasons.append("TOKEN_MAPPING_NOT_DISTINCT")
    if rules.neg_risk is None:
        reasons.append("NEG_RISK_STATUS_UNKNOWN")
    elif rules.neg_risk:
        reasons.append("NEGATIVE_RISK_MARKET_NOT_BINARY_COMPLEMENT")
    if not rules.rule_hash:
        reasons.append("RULE_HASH_MISSING")
    if not rules.is_real_money:
        reasons.append("PLAY_MONEY_NOT_EXECUTABLE")
    return reasons


def verify_cross_venue(left: ContractRules, right: ContractRules) -> Equivalence:
    """Explicit event/oracle/cutoff/outcome/currency/settlement parity or no parity.

    Unknown fields yield ``REVIEW_ONLY`` (a human must confirm). Known but
    different fields yield ``MISMATCHED``. Title similarity is not an input.
    """
    reasons: list[str] = []
    for side in (left, right):
        if not side.is_real_money:
            reasons.append(f"PLAY_MONEY_LEG:{side.venue}")
    if reasons:
        return Equivalence(status="NON_EXECUTABLE", reasons=reasons, checked=list(_CROSS_FIELDS))
    if left.venue == right.venue and left.market_id == right.market_id:
        return Equivalence(
            status="MISMATCHED", reasons=["SAME_CONTRACT_NOT_CROSS_VENUE"], checked=[]
        )
    mismatched: list[str] = []
    unknown: list[str] = []
    for field in _CROSS_FIELDS:
        a, b = getattr(left, field), getattr(right, field)
        if a is None or b is None or a == "" or b == "":
            unknown.append(f"{field.upper()}_UNVERIFIED")
        elif a != b:
            mismatched.append(f"{field.upper()}_MISMATCH")
    if mismatched:
        return Equivalence(
            status="MISMATCHED", reasons=mismatched + unknown, checked=list(_CROSS_FIELDS)
        )
    if unknown:
        return Equivalence(status="REVIEW_ONLY", reasons=unknown, checked=list(_CROSS_FIELDS))
    return Equivalence(status="VERIFIED_EQUIVALENT", reasons=[], checked=list(_CROSS_FIELDS))


class Scenario(Contract):
    name: str
    states: tuple[str, ...]
    payouts: tuple[Decimal, ...]
    total: Decimal


class PayoffMatrix(Contract):
    version: str = PAYOFF_MATRIX_VERSION
    legs: tuple[str, ...]
    scenarios: list[Scenario]
    exhaustive: bool
    min_total: Decimal
    max_total: Decimal
    verified_equivalent: bool


def _leg_payout(contract_state: str, leg_outcome: str) -> Decimal:
    return _PAYOUT[(contract_state, leg_outcome)] if contract_state != "VOID" else Decimal("0.5")


def build_payoff_matrix(legs: Sequence[tuple[str, str, int]], *, equivalent: bool) -> PayoffMatrix:
    """Scenario matrix for a basket of 1-share legs.

    ``legs`` is ``(label, outcome, contract_index)``: the outcome bought (YES or
    NO) and which independent contract resolves it. Legs sharing a contract
    index resolve together. When ``equivalent`` is False the contracts resolve
    independently, so every combination of states is a legal scenario; when
    True (verified equivalent) all contracts share one state.
    """
    contract_ids = sorted({index for _, _, index in legs})
    if equivalent or len(contract_ids) == 1:
        combos = [dict.fromkeys(contract_ids, state) for state in STATES]
    else:
        combos = [
            dict(zip(contract_ids, c, strict=True))
            for c in product(STATES, repeat=len(contract_ids))
        ]
    scenarios: list[Scenario] = []
    with localcontext(ACCOUNTING_CONTEXT):
        for combo in combos:
            payouts = tuple(_leg_payout(combo[idx], outcome) for _, outcome, idx in legs)
            scenarios.append(
                Scenario(
                    name="|".join(f"c{i}={combo[i]}" for i in contract_ids),
                    states=tuple(combo[i] for i in contract_ids),
                    payouts=payouts,
                    total=sum(payouts, Decimal(0)),
                )
            )
        totals = [s.total for s in scenarios]
    return PayoffMatrix(
        legs=tuple(label for label, _, _ in legs),
        scenarios=scenarios,
        exhaustive=True,
        min_total=min(totals),
        max_total=max(totals),
        verified_equivalent=bool(equivalent or len(contract_ids) == 1),
    )


def validate_matrix(matrix: PayoffMatrix) -> list[str]:
    """Reasons a matrix is unusable: empty, non-exhaustive, or negative payout."""
    reasons: list[str] = []
    if not matrix.scenarios or not matrix.exhaustive:
        reasons.append("PAYOFF_MATRIX_NOT_EXHAUSTIVE")
    for scenario in matrix.scenarios:
        if any(p < 0 or p > 1 for p in scenario.payouts):
            reasons.append("PAYOUT_OUT_OF_RANGE")
            break
    return reasons
