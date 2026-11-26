"""Quote consistency: a deterministic, network-less, read-only research skill.

Given one frozen snapshot of the YES and NO quotes of a binary market, it
reports descriptive features (mid, spread, YES+NO overround, book skew, quote
age, gap to an optional external reference) and the interval the quoted book
itself implies. It never produces a probability forecast: probability is
always ``None`` because nothing here is calibrated. It abstains, with a
machine-readable reason, whenever the context is stale, ambiguous or
inconsistent.

This module is pure Python with no I/O, no clock, no environment access and no
imports beyond the standard library. It has no order, signing or credential
surface and is verified against the pinned manifest hash before every run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

MAX_QUOTE_AGE_SECONDS = 300.0
MAX_REFERENCE_AGE_SECONDS = 900.0
CONSISTENCY_TOLERANCE = 0.02


def _epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _abstain(reason: str, features: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "features": features or {},
        "probability": None,
        "interval": None,
        "abstain_reason": reason,
        "warnings": [],
    }


def evaluate(context: dict[str, Any]) -> dict[str, Any]:
    """Return SkillEvidence fields for one validated SkillContext."""
    if context["data_quality"] != "ok":
        return _abstain("DATA_QUALITY_" + context["data_quality"].upper())
    if not context.get("rule_hash"):
        return _abstain("MISSING_RULE_HASH")

    decision = _epoch(context["decision_time_utc"])
    yes, no = context["quotes"]["yes"], context["quotes"]["no"]
    ages = {}
    for side, quote in (("yes", yes), ("no", no)):
        if quote["bid"] > quote["ask"]:
            return _abstain("CROSSED_BOOK")
        observed = _epoch(quote["observed_at_utc"])
        if observed > decision:
            return _abstain("QUOTE_AFTER_DECISION")
        ages[side] = decision - observed
    quote_age = max(ages.values())
    features: dict[str, Any] = {"quote_age_seconds": round(quote_age, 3)}
    if quote_age > MAX_QUOTE_AGE_SECONDS:
        return _abstain("STALE_QUOTE", features)

    yes_mid = (yes["bid"] + yes["ask"]) / 2
    no_mid = (no["bid"] + no["ask"]) / 2
    # A complete binary book should satisfy YES + NO ~= 1; the gap is reported,
    # never "corrected" into a trade.
    overround = yes_mid + no_mid - 1.0
    features.update(
        {
            "yes_mid": round(yes_mid, 6),
            "no_mid": round(no_mid, 6),
            "yes_spread": round(yes["ask"] - yes["bid"], 6),
            "no_spread": round(no["ask"] - no["bid"], 6),
            "yes_no_sum_minus_one": round(overround, 6),
            "complement_consistent": abs(overround) <= CONSISTENCY_TOLERANCE,
            "implied_yes_from_no_mid": round(1.0 - no_mid, 6),
        }
    )
    warnings: list[str] = []
    reference = context.get("external_reference")
    if reference is not None:
        ref_age = decision - _epoch(reference["observed_at_utc"])
        if ref_age < 0:
            return _abstain("REFERENCE_AFTER_DECISION", features)
        if ref_age > MAX_REFERENCE_AGE_SECONDS:
            return _abstain("STALE_EXTERNAL_REFERENCE", features)
        features["external_reference_source"] = reference["source"]
        features["external_reference_age_seconds"] = round(ref_age, 3)
        features["yes_mid_minus_external_reference"] = round(yes_mid - reference["implied_yes"], 6)
    if not features["complement_consistent"]:
        warnings.append("YES_NO_QUOTES_INCONSISTENT")

    return {
        "features": features,
        # Never a forecast: no calibration exists for this skill.
        "probability": None,
        "interval": {
            "low": round(yes["bid"], 6),
            "high": round(yes["ask"], 6),
            "basis": "quoted_yes_bid_ask",
        },
        "abstain_reason": None,
        "warnings": warnings,
    }
