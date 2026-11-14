"""Pure resolution of explicitly supported, verified point-in-time contracts."""

import re
from datetime import UTC
from decimal import Decimal

from app.services.intelligence.resolution_rule_models import (
    ContractSpecification,
    PayoffObservation,
    PayoffResolution,
)


def resolve_payoff(spec: ContractSpecification, observation: PayoffObservation) -> PayoffResolution:
    """Pure deterministic payoff resolution over declared states.

    Categorically abstains if the rule specification is unverified or ambiguous.
    """
    if spec.review_status != "verified":
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="UNVERIFIED_OR_AMBIGUOUS_RULE",
        )

    if not spec.oracle or spec.oracle.casefold() != observation.oracle.strip().casefold():
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="ORACLE_SOURCE_MISMATCH",
        )

    if (
        spec.oracle_symbol
        and spec.oracle_symbol.upper() != (observation.oracle_symbol or "").upper()
    ):
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="ORACLE_SYMBOL_MISMATCH",
        )

    if observation.observed_at.tzinfo is None or (
        observation.source_at is not None and observation.source_at.tzinfo is None
    ):
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="NAIVE_ORACLE_TIMESTAMP",
        )
    obs_utc = observation.observed_at.astimezone(UTC)
    if observation.source_at is None:
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="MISSING_ORACLE_EVENT_TIME",
        )
    source_at = observation.source_at.astimezone(UTC)
    if source_at > obs_utc or source_at != spec.cutoff_utc:
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="ORACLE_CUTOFF_MISMATCH",
        )
    if (
        spec.cutoff_utc is not None
        and obs_utc < spec.cutoff_utc
        and not observation.early_finalized
    ):
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="OBSERVATION_BEFORE_CUTOFF",
        )

    uma_state = (observation.uma_status or "").strip().lower()
    if uma_state in ("void", "voided"):
        if not re.search(r"\b50\s*(?:/|-)\s*50\b", spec.raw_rules):
            return PayoffResolution(
                condition_id=spec.condition_id,
                source_hash=spec.source_hash,
                outcome="ABSTAIN",
                reason="UNVERIFIED_VOID_PAYOUT",
            )
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="VOID",
            payout_yes=Decimal("0.5"),
            payout_no=Decimal("0.5"),
            reason="VENUE_VOID_SETTLEMENT",
        )
    if uma_state in ("disputed", "challenged"):
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="ACTIVE_DISPUTE",
        )

    if observation.value is None or spec.threshold is None or spec.comparator == "unknown":
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="MISSING_QUANTITATIVE_INPUT",
        )

    val = observation.value
    if not val.is_finite():
        return PayoffResolution(
            condition_id=spec.condition_id,
            source_hash=spec.source_hash,
            outcome="ABSTAIN",
            reason="NONFINITE_ORACLE_VALUE",
        )
    thresh = spec.threshold
    if spec.comparator == ">":
        yes_wins = val > thresh
    elif spec.comparator == ">=":
        yes_wins = val >= thresh
    elif spec.comparator == "<":
        yes_wins = val < thresh
    elif spec.comparator == "<=":
        yes_wins = val <= thresh
    else:
        yes_wins = val == thresh

    return PayoffResolution(
        condition_id=spec.condition_id,
        source_hash=spec.source_hash,
        outcome="YES" if yes_wins else "NO",
        payout_yes=Decimal("1") if yes_wins else Decimal("0"),
        payout_no=Decimal("0") if yes_wins else Decimal("1"),
        reason="DETERMINISTIC_COMPARATOR_MATCH",
    )
