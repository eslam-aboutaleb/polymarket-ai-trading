"""Deterministic, Decimal-exact oracle basis risk analysis (#17).

Compares external spot/reference feeds (e.g. Binance/Kraken) against
authoritative Polymarket settlement observations without ever substituting
spot candles for authoritative settlement. Hard-abstains when settlement
feeds are missing, stale, or lack a verified resolution rule (#16).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    localcontext,
)
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

SKILL_VERSION = "oracle-basis-risk-v1"
SCHEMA_VERSION = "oracle-basis-v1"


class OracleSpecification(BaseModel):
    """Authoritative settlement oracle specification pinned to a rule hash."""

    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    rule_hash: str
    rule_review_status: Literal["verified", "unverified", "ambiguous", "invalid"]
    authoritative_provider: str
    eligible_instrument: str
    aggregation: Literal["last", "median", "twap"] = "last"
    sampling_interval_seconds: int = Field(default=60, gt=0)
    rounding_decimals: int = Field(default=2, ge=0, le=12)
    cutoff_utc: datetime
    window_start_utc: datetime | None = None
    window_seconds: int = Field(default=60, gt=0, le=3600)
    rounding_mode: Literal["none", "half_up", "half_even", "floor", "ceiling"] = "none"
    fallback_provider: str | None = None


class FeedObservation(BaseModel):
    """Single point-in-time observation from either spot or settlement feed."""

    model_config = ConfigDict(extra="forbid")

    source: str
    instrument: str | None = None
    capture_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_at: datetime
    received_at: datetime
    value: Decimal


class OracleBasisObservation(BaseModel):
    """Time-aligned pair of spot vs authoritative settlement observation."""

    model_config = ConfigDict(extra="forbid")

    condition_id: str
    oracle_source: str
    spot_feed: str
    source_at: datetime
    received_at: datetime
    reference_value: Decimal
    feed_value: Decimal
    basis_bp: Decimal
    latency_ms: int


class OracleBasisStudy(BaseModel):
    """Read-only oracle basis distribution and threshold reversal risk."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["oracle-basis-v1"] = SCHEMA_VERSION
    skill_version: str = SKILL_VERSION
    condition_id: str
    rule_hash: str
    oracle_verified: bool
    status: Literal["analyzed", "abstain"]
    abstain_reasons: list[str] = Field(default_factory=list)
    paired_count: int = 0
    missing_settlement_count: int = 0
    stale_settlement_count: int = 0
    basis_mean_bp: Decimal | None = None
    basis_p95_abs_bp: Decimal | None = None
    basis_max_abs_bp: Decimal | None = None
    sign_reversal_count: int = 0
    false_win_rate: Decimal | None = None
    observations: list[OracleBasisObservation] = Field(default_factory=list)
    false_win_interval_95: tuple[Decimal, Decimal] | None = None
    spot_positive_count: int = 0
    classification_error_count: int = 0
    raw_point_audit: list[dict] = Field(default_factory=list)
    evidence_class: str = "caller_supplied_diagnostic_not_authenticated_oracle"
    feed_checksum_sha256: str
    decision: Literal["research_only"] = "research_only"
    execution_eligible: Literal[False] = False


class OracleBasisRequest(BaseModel):
    """Input payload for analyzing oracle basis risk."""

    model_config = ConfigDict(extra="forbid")

    specification: OracleSpecification
    as_of: datetime | None = None
    strike: Decimal
    comparator: Literal[">", ">=", "<", "<="] = ">="
    spot_observations: list[FeedObservation] = Field(default_factory=list, max_length=5000)
    settlement_observations: list[FeedObservation] = Field(default_factory=list, max_length=5000)
    max_alignment_skew_seconds: int = Field(default=60, ge=0, le=3600)
    max_settlement_lag_seconds: int = Field(default=300, ge=0, le=86400)


def _wins(value: Decimal, strike: Decimal, comparator: Literal[">", ">=", "<", "<="]) -> bool:
    if comparator == ">":
        return value > strike
    if comparator == ">=":
        return value >= strike
    if comparator == "<":
        return value < strike
    return value <= strike


def _feed_checksum(req: OracleBasisRequest) -> str:
    payload = json.loads(req.model_dump_json())
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_oracle_basis(
    req: OracleBasisRequest, *, lineage_verified: bool = False
) -> OracleBasisStudy:
    """Time-aligned diagnostics; only a server-side archived lineage can verify an oracle."""
    with localcontext(ACCOUNTING_CONTEXT):
        return _evaluate(req, lineage_verified)


def _evaluate(req, lineage_verified):
    spec = req.specification
    checksum = _feed_checksum(req)
    reasons = []

    def result(**kwargs):
        return OracleBasisStudy(
            condition_id=spec.condition_id.lower(),
            rule_hash=spec.rule_hash,
            feed_checksum_sha256=checksum,
            **kwargs,
        )

    if spec.cutoff_utc.tzinfo is None or (req.as_of is not None and req.as_of.tzinfo is None):
        reasons.append("ABSTAIN_NAIVE_CUTOFF")
    if spec.window_start_utc is not None and spec.window_start_utc.tzinfo is None:
        reasons.append("ABSTAIN_NAIVE_WINDOW")
    if spec.rule_review_status != "verified":
        reasons.append("ABSTAIN_UNVERIFIED_RULE")
    if not spec.rule_hash.strip():
        reasons.append("ABSTAIN_MISSING_RULES_HASH")
    if not req.settlement_observations:
        reasons.append("ABSTAIN_MISSING_SETTLEMENT_FEED")
    if not req.spot_observations:
        reasons.append("ABSTAIN_MISSING_SPOT_FEED")
    points = [*req.spot_observations, *req.settlement_observations]
    if any(p.source_at.tzinfo is None or p.received_at.tzinfo is None for p in points):
        reasons.append("ABSTAIN_NAIVE_TIMESTAMP")
    if not req.strike.is_finite():
        reasons.append("ABSTAIN_INVALID_STRIKE")
    if any(not p.value.is_finite() or p.value <= 0 for p in points):
        reasons.append("ABSTAIN_INVALID_FEED_VALUE")
    if reasons:
        return result(
            status="abstain", oracle_verified=False, abstain_reasons=list(dict.fromkeys(reasons))
        )
    cutoff = spec.cutoff_utc.astimezone(UTC)
    as_of = (req.as_of or cutoff).astimezone(UTC)
    start = spec.window_start_utc.astimezone(UTC) if spec.window_start_utc else None
    if start is not None and start >= cutoff:
        return result(
            status="abstain", oracle_verified=False, abstain_reasons=["ABSTAIN_INVALID_WINDOW"]
        )
    if any(p.source_at > p.received_at or p.received_at > as_of for p in points):
        return result(
            status="abstain",
            oracle_verified=False,
            abstain_reasons=["ABSTAIN_FUTURE_OR_UNAVAILABLE_POINT"],
        )
    if any(p.instrument != spec.eligible_instrument for p in points):
        return result(
            status="abstain",
            oracle_verified=False,
            abstain_reasons=["ABSTAIN_ORACLE_INSTRUMENT_MISMATCH"],
        )
    if any(
        p.source.strip().casefold() != spec.authoritative_provider.strip().casefold()
        for p in req.settlement_observations
    ):
        return result(
            status="abstain",
            oracle_verified=False,
            abstain_reasons=["ABSTAIN_ORACLE_PROVIDER_MISMATCH"],
        )
    if not spec.authoritative_provider.strip() or not spec.eligible_instrument.strip():
        return result(
            status="abstain",
            oracle_verified=False,
            abstain_reasons=["ABSTAIN_MISSING_ORACLE_IDENTITY"],
        )
    settlement = sorted(req.settlement_observations, key=lambda p: p.source_at)
    if len({p.source_at for p in settlement}) != len(settlement):
        return result(
            status="abstain",
            oracle_verified=False,
            abstain_reasons=["ABSTAIN_DUPLICATE_OR_CONFLICTING_TIMESTAMP"],
        )
    modes = {
        "half_up": ROUND_HALF_UP,
        "half_even": ROUND_HALF_EVEN,
        "floor": ROUND_FLOOR,
        "ceiling": ROUND_CEILING,
    }
    rounding = modes.get(spec.rounding_mode)
    quantum = Decimal(10) ** -spec.rounding_decimals
    pairs, audit = [], []
    audit_point_count = 0
    missing = stale = false_wins = positives = errors = 0
    for spot in sorted(req.spot_observations, key=lambda p: (p.source_at, p.received_at)):
        end = spot.source_at.astimezone(UTC)
        if end > cutoff or (start is not None and end < start):
            missing += 1
            continue
        # A reference published later than this spot observation is unavailable
        # at that decision. Alignment tolerance never authorizes future knowledge.
        available = [
            p for p in settlement if p.source_at <= end and p.received_at <= spot.received_at
        ]
        if not available:
            missing += 1
            continue
        latest = available[-1]
        if (
            (end - latest.source_at).total_seconds() > req.max_alignment_skew_seconds
            or (latest.received_at - latest.source_at).total_seconds()
            > req.max_settlement_lag_seconds
            or (spot.received_at - latest.received_at).total_seconds()
            > req.max_settlement_lag_seconds
        ):
            stale += 1
            continue
        used = [latest]
        if spec.aggregation == "last":
            reference = latest.value
        else:
            begin = end - timedelta(seconds=spec.window_seconds)
            prior = [p for p in available if p.source_at <= begin]
            if (
                not prior
                or (begin - prior[-1].source_at).total_seconds() > spec.sampling_interval_seconds
            ):
                missing += 1
                continue
            used = [prior[-1], *[p for p in available if begin < p.source_at <= end]]
            boundaries = [*[p.source_at for p in used], end]
            if any(
                (b - a).total_seconds() > spec.sampling_interval_seconds
                for a, b in zip(boundaries, boundaries[1:], strict=False)
            ):
                missing += 1
                continue
            if spec.aggregation == "median":
                reference = Decimal(median([p.value for p in used]))
            else:
                weighted = Decimal(0)
                for index, point in enumerate(used):
                    left = max(begin, point.source_at)
                    right = used[index + 1].source_at if index + 1 < len(used) else end
                    micros = (right - left) // timedelta(microseconds=1)
                    weighted += point.value * Decimal(micros)
                reference = weighted / Decimal(spec.window_seconds * 1_000_000)
        audit_point_count += len(used) + 1
        if audit_point_count > 2000:
            return result(
                status="abstain",
                oracle_verified=False,
                abstain_reasons=["ABSTAIN_AUDIT_RUNTIME_BOUND"],
            )
        reference = reference.quantize(quantum, rounding=rounding) if rounding else reference
        spot_value = spot.value.quantize(quantum, rounding=rounding) if rounding else spot.value
        if reference <= 0:
            missing += 1
            continue
        sw, rw = (
            _wins(spot_value, req.strike, req.comparator),
            _wins(reference, req.strike, req.comparator),
        )
        positives += int(sw)
        false_wins += int(sw and not rw)
        errors += int(sw != rw)
        basis = (spot_value - reference) / reference * Decimal(10000)
        pairs.append(
            OracleBasisObservation(
                condition_id=spec.condition_id.lower(),
                oracle_source=latest.source,
                spot_feed=spot.source,
                source_at=end,
                received_at=spot.received_at.astimezone(UTC),
                reference_value=reference,
                feed_value=spot_value,
                basis_bp=basis.quantize(Decimal(".01")),
                latency_ms=(latest.received_at - latest.source_at) // timedelta(milliseconds=1),
            )
        )
        audit.append(
            {
                "spot": spot.model_dump(mode="json"),
                "settlement_points": [p.model_dump(mode="json") for p in used],
                "available_at": spot.received_at.isoformat(),
                "aggregation": spec.aggregation,
                "rounding": spec.rounding_mode,
                "window_seconds": spec.window_seconds,
            }
        )
    if missing:
        reasons.append("ABSTAIN_INCOMPLETE_SETTLEMENT_HISTORY")
    if stale:
        reasons.append("ABSTAIN_STALE_SETTLEMENT_FEED")
    if not pairs:
        reasons.append("ABSTAIN_NO_ALIGNED_OBSERVATIONS")
    if not lineage_verified:
        reasons.append("ABSTAIN_UNVERIFIED_SETTLEMENT_LINEAGE")
    absolute = sorted(abs(p.basis_bp) for p in pairs)
    interval = None
    false_rate = Decimal(false_wins) / positives if positives else None
    if positives:
        n, z2 = Decimal(positives), Decimal("1.96") ** 2
        centre = (false_rate + z2 / (2 * n)) / (1 + z2 / n)
        radius = (
            Decimal("1.96")
            * (false_rate * (1 - false_rate) / n + z2 / (4 * n * n)).sqrt()
            / (1 + z2 / n)
        )
        interval = (max(Decimal(0), centre - radius), min(Decimal(1), centre + radius))
    verified = lineage_verified and not reasons
    return result(
        status="analyzed" if verified else "abstain",
        oracle_verified=verified,
        abstain_reasons=reasons,
        paired_count=len(pairs),
        missing_settlement_count=missing,
        stale_settlement_count=stale,
        basis_mean_bp=sum((p.basis_bp for p in pairs), Decimal(0)) / len(pairs) if pairs else None,
        basis_p95_abs_bp=absolute[(95 * len(absolute) + 99) // 100 - 1] if absolute else None,
        basis_max_abs_bp=absolute[-1] if absolute else None,
        sign_reversal_count=false_wins,
        false_win_rate=false_rate,
        false_win_interval_95=interval,
        spot_positive_count=positives,
        classification_error_count=errors,
        observations=pairs,
        raw_point_audit=audit,
        evidence_class="archived_source_verified_diagnostic"
        if verified
        else "unverified_diagnostic",
    )
