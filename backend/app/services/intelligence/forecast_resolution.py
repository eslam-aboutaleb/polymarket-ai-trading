"""Nightly forecast resolution + Brier scoring (plan 01).

Closes the calibration loop: for every unresolved AI verdict that
carries an ``ai_probability``, look up how the market actually
resolved on Polymarket and write ``resolved_outcome``,
``brier_score = (p_yes - outcome)^2`` and ``resolved_at``.

Resolution data source (verified live with curl on 2026-10-11)::

    GET https://gamma-api.polymarket.com/markets?closed=true&condition_ids=<cid>

The response is a JSON array of market objects where:

* ``conditionId`` is the exact-match market key. The camelCase
  ``conditionId`` *query* parameter performs fuzzy matching (it
  returns unrelated markets), so the plural ``condition_ids``
  parameter must be used instead. Repeating the parameter
  (``...&condition_ids=A&condition_ids=B``) also works for batching;
  this module issues one request per condition id for clarity.
* ``outcomes`` and ``outcomePrices`` are **JSON-encoded strings**
  (``"[\\"Yes\\", \\"No\\"]"`` / ``"[\\"1\\", \\"0\\"]"``) and must be
  decoded with ``json.loads`` before use.
* The winning outcome must be a uniquely mapped YES/NO binary pair whose
  final CLOB settlement value is one/zero, with explicit finalized UMA status.
  Merely closed markets, 50/50 prices, nonbinary markets and unsettled markets
  are excluded from accuracy metrics.
* ``closed=true`` restricts the lookup to settled markets.

Brier convention: only an unambiguous finalized YES or NO winner can be
scored against the YES forecast probability. All other outcome labels, null
outcomes, nonfinite probabilities and unfinalized states are excluded.

Scoring version: every scored row is stamped with ``SCORING_VERSION``
(``"brier-binary-v2"``) so a change of the scoring rule is distinguishable
from the rows written under the previous one.

Exclusion reasons: a row that cannot be scored is never silently dropped.
The reason is one of the :class:`ExclusionReason` values, it is recorded on
the row (``Assessment.exclusion_reason``) and it is counted per reason on
the resolution run, so "unresolved" can be told apart from "disputed",
"void", "multi outcome", "ambiguous settlement", "wrong market",
"unusable probability" and "forecast recorded after the market closed".

Leakage guard: a forecast recorded *after* the market closed already knows
the outcome, so it is excluded (``forecast_after_close``) instead of being
scored against the resolution it could have anticipated.

Idempotency: ``resolve_pending`` only touches rows where
``resolved_at IS NULL``, so a re-run (the advisory lock fails open on
database errors, so exclusivity is never assumed) scores each market
at most once.
"""

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.analytics.assessment import (
    BINARY_NO_LABELS,
    BINARY_YES_LABELS,
    VERIFIED_SCORING_VERSION,
)
from app.repositories.intelligence.assessments import AssessmentRepository
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)
settings = get_settings()

SCHEDULER_NAME = "forecast_resolution"

#: Version of the scoring rule that produced a Brier score. A row written
#: under an older rule is distinguishable from one written under this one.
SCORING_VERSION = VERIFIED_SCORING_VERSION

#: Clipping applied to a probability *inside* the log-loss computation.
#: The stored ``ai_probability`` is never rewritten: a stated certainty
#: (p = 0 or p = 1) stays exactly as the forecaster stated it.
LOG_LOSS_EPSILON = 1e-6

# Verified live shape — see the module docstring.
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_URL = f"{GAMMA_API_BASE}/markets"
_RESOLUTION_TIMEOUT = 20.0
_RESOLUTION_BATCH_LIMIT = 200

# How many Gamma lookups one resolution pass runs at once. Without a cap
# the 200-row batch is a 200-request serial walk — a slow Gamma can hold
# the pass for well over an hour — so the walk is chunked into bounded
# concurrent groups instead.
_RESOLUTION_CONCURRENCY = 8

# Coin-flip reference score: a perpetually 50/50 forecast.
BASELINE_BRIER = 0.25

# Yes-like labels map to outcome 1.0; no-like labels map to outcome 0.0.
# Anything else is a legacy/nonbinary label and can never become a label.
_YES_LABELS = BINARY_YES_LABELS
_NO_LABELS = BINARY_NO_LABELS

# UMA settlement states. Only a finalized one can be scored.
_FINAL_STATUSES = {"resolved", "settled"}
_DISPUTED_STATUSES = {"disputed", "challenged"}
_VOID_STATUSES = {"voided", "void"}

_resolution_task: asyncio.Task | None = None


class ExclusionReason(StrEnum):
    """Why a forecast row could not be scored against a resolution."""

    #: The market was closed but never reached a final UMA state.
    CLOSED_NOT_FINALIZED = "closed_not_finalized"
    #: The resolution is disputed or was challenged — the outcome is not final.
    DISPUTED_OR_CHALLENGED = "disputed_or_challenged"
    #: The market was voided; there is no outcome to score.
    VOID = "void"
    #: Not a two-outcome market (or more than two outcomes).
    MULTI_OUTCOME = "multi_outcome"
    #: Two outcomes that are not a YES/NO pair.
    NON_BINARY_OUTCOME = "non_binary_outcome"
    #: Settlement values are ambiguous (no winner, 50/50, out of range).
    AMBIGUOUS_SETTLEMENT = "ambiguous_settlement"
    #: Gamma answered with a different (or several) markets than asked.
    CONDITION_ID_MISMATCH = "condition_id_mismatch"
    #: The market was not found at all.
    MARKET_NOT_FOUND = "market_not_found"
    #: The lookup itself failed (transport error, non-200 response).
    LOOKUP_FAILED = "lookup_failed"
    #: The stored probability is missing or outside ``[0, 1]``.
    INVALID_PROBABILITY = "invalid_probability"
    #: The forecast was recorded after the market had already closed.
    FORECAST_AFTER_CLOSE = "forecast_after_close"


@dataclass
class ResolutionRun:
    """Summary of one resolution pass."""

    checked: int = 0
    resolved: int = 0
    brier_sum: float = 0.0
    unresolved_condition_ids: list[str] = field(default_factory=list)
    #: Exclusion reason -> number of rows excluded for that reason.
    exclusions: dict[str, int] = field(default_factory=dict)
    #: Number of *markets* scored (one effective sample per market).
    effective_resolved: int = 0
    #: Brier sum over the effective (one-per-market) samples.
    effective_brier_sum: float = 0.0
    _scored_conditions: set[str] = field(default_factory=set, repr=False)

    @property
    def mean_brier(self) -> float | None:
        return self.brier_sum / self.resolved if self.resolved else None

    @property
    def effective_mean_brier(self) -> float | None:
        if not self.effective_resolved:
            return None
        return self.effective_brier_sum / self.effective_resolved

    def record_sample(self, condition_id: str | None, score: float) -> None:
        """Count one scored row, but a market only once.

        Two rows for the same market (a re-analysis, a second model) are
        two forecasts but one market: the aggregate must see the market
        once, while the raw row count stays visible.
        """
        key = (condition_id or "").strip()
        if key:
            if key in self._scored_conditions:
                return
            self._scored_conditions.add(key)
        self.effective_resolved += 1
        self.effective_brier_sum += score

    def exclude(self, condition_id: str | None, reason: ExclusionReason) -> None:
        """Record one excluded row and the reason it was excluded."""
        self.unresolved_condition_ids.append(condition_id or "")
        key = reason.value
        self.exclusions[key] = self.exclusions.get(key, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "resolved": self.resolved,
            "mean_brier": None if self.mean_brier is None else round(self.mean_brier, 4),
            "unresolved": len(self.unresolved_condition_ids),
            "excluded": sum(self.exclusions.values()),
            "exclusion_reasons": dict(self.exclusions),
            "effective_sample_count": self.effective_resolved,
            "effective_mean_brier": (
                None if self.effective_mean_brier is None else round(self.effective_mean_brier, 4)
            ),
        }


#: The last pass executed by this process (see ``last_resolution_run``).
_last_run: "ResolutionRun | None" = None


@dataclass(frozen=True)
class MarketResolution:
    """What Gamma reports about one market.

    Exactly one of ``outcome`` and ``reason`` is set: a market is either
    scoreable (``outcome``) or excluded with a reason (``reason``).
    ``closed_at`` is the market's close time when Gamma reported one; it
    is the reference the leakage guard compares a forecast against.
    """

    outcome: str | None = None
    reason: ExclusionReason | None = None
    closed_at: datetime | None = None

    @property
    def is_resolved(self) -> bool:
        return self.outcome is not None


def _decode_json_string(raw: Any) -> list:
    """Decode a Gamma JSON-encoded string field (``outcomes``/``outcomePrices``)."""
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a unix timestamp or an ISO-8601 string into an aware datetime."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def market_close_time(market: dict[str, Any]) -> datetime | None:
    """The moment the market closed, as Gamma reports it.

    ``closedTime`` is the actual close (unix seconds) and is preferred;
    ``endDate`` is the scheduled end and is the fallback. A forecast
    recorded after this moment already knew the outcome, so it must not
    be scored against the resolution.
    """
    if not isinstance(market, dict):
        return None
    closed = market.get("closedTime")
    if closed not in (None, ""):
        parsed = _parse_timestamp(closed)
        if parsed is not None:
            return parsed
    for key in ("endDate", "end_date", "endDateIso"):
        parsed = _parse_timestamp(market.get(key))
        if parsed is not None:
            return parsed
    return None


def classify_market(market: dict[str, Any]) -> MarketResolution:
    """Classify one Gamma market into a scoreable outcome or a reason.

    Closing a market is not settlement finality. Ambiguous payouts,
    nonbinary outcomes, partial settlement and malformed response data
    cannot become training/quality labels, and each of them has its own
    reason so the failure modes stay distinguishable.
    """
    if not isinstance(market, dict):
        return MarketResolution(reason=ExclusionReason.CONDITION_ID_MISMATCH)
    status = str(market.get("umaResolutionStatus", "")).strip().lower()
    closed_at = market_close_time(market)
    if status in _VOID_STATUSES:
        return MarketResolution(reason=ExclusionReason.VOID, closed_at=closed_at)
    if status in _DISPUTED_STATUSES:
        return MarketResolution(reason=ExclusionReason.DISPUTED_OR_CHALLENGED, closed_at=closed_at)
    if status not in _FINAL_STATUSES:
        return MarketResolution(reason=ExclusionReason.CLOSED_NOT_FINALIZED, closed_at=closed_at)
    if market.get("closed") not in (True, "true"):
        return MarketResolution(reason=ExclusionReason.CLOSED_NOT_FINALIZED, closed_at=closed_at)
    outcomes = _decode_json_string(market.get("outcomes"))
    prices = _decode_json_string(market.get("outcomePrices"))
    if len(outcomes) != 2 or len(prices) != 2:
        return MarketResolution(reason=ExclusionReason.MULTI_OUTCOME, closed_at=closed_at)
    labels = [str(v).strip().lower() for v in outcomes]
    if set(labels) != {"yes", "no"}:
        return MarketResolution(reason=ExclusionReason.NON_BINARY_OUTCOME, closed_at=closed_at)
    try:
        values = [float(v) for v in prices]
    except (TypeError, ValueError):
        return MarketResolution(reason=ExclusionReason.AMBIGUOUS_SETTLEMENT, closed_at=closed_at)
    if any(not math.isfinite(v) or v < 0 or v > 1 for v in values):
        return MarketResolution(reason=ExclusionReason.AMBIGUOUS_SETTLEMENT, closed_at=closed_at)
    if values == [1.0, 0.0]:
        return MarketResolution(outcome=labels[0].upper(), closed_at=closed_at)
    if values == [0.0, 1.0]:
        return MarketResolution(outcome=labels[1].upper(), closed_at=closed_at)
    return MarketResolution(reason=ExclusionReason.AMBIGUOUS_SETTLEMENT, closed_at=closed_at)


def winning_outcome(market: dict[str, Any]) -> str | None:
    """The finalized YES/NO winner of a canonical binary market, else ``None``."""
    return classify_market(market).outcome


def is_binary_outcome(resolved_outcome: str | None) -> bool:
    """True when the label is a finalized binary YES/NO label.

    The non-raising sibling of :func:`outcome_value`: read paths use it
    to *skip* a row whose label is not a verified binary outcome (a legacy
    resolver stored the raw Gamma label, e.g. "ALICE") instead of
    raising on it.
    """
    label = (resolved_outcome or "").strip().lower()
    return label in _YES_LABELS or label in _NO_LABELS


def is_verified_resolution(
    brier_score: float | None,
    resolved_outcome: str | None,
    scoring_version: str | None = None,
) -> bool:
    """True when a row carries a scoreable, verified binary resolution.

    A binary label alone does not prove finality: the legacy resolver also
    scored merely closed markets. Only the current resolver's version stamp
    and a finite bounded score establish eligibility. Old evidence stays
    readable but cannot become verified calibration data.
    """
    if scoring_version != SCORING_VERSION or brier_score is None or resolved_outcome is None:
        return False
    return (
        math.isfinite(brier_score) and 0 <= brier_score <= 1 and is_binary_outcome(resolved_outcome)
    )


def outcome_value(resolved_outcome: str | None) -> float:
    """Map only known binary outcomes, raising on unverified labels."""
    label = (resolved_outcome or "").strip().lower()
    if label in _YES_LABELS:
        return 1.0
    if label in _NO_LABELS:
        return 0.0
    raise ValueError("Forecast Brier outcome must be finalized YES or NO")


def brier_score(ai_probability: float, resolved_outcome: str | None) -> float:
    """Proper binary Brier score: reject missing/nonfinite p and labels."""
    probability = float(ai_probability)
    if not math.isfinite(probability) or not (0 <= probability <= 1):
        raise ValueError("Forecast probability must be finite between 0 and 1")
    error = probability - outcome_value(resolved_outcome)
    return error * error


def log_loss(ai_probability: float, resolved_outcome: str | None) -> float:
    """Binary log loss with the probability clipped for the computation only.

    A stated certainty (p = 0 or p = 1) is a legitimate forecast and is
    kept as stated; the logarithm is the only thing that needs a finite
    argument, so the probability is clipped to ``[eps, 1 - eps]`` here.
    The stored ``ai_probability`` is never rewritten.
    """
    probability = float(ai_probability)
    if not math.isfinite(probability) or not (0 <= probability <= 1):
        raise ValueError("Forecast probability must be finite between 0 and 1")
    outcome = outcome_value(resolved_outcome)
    clipped = min(max(probability, LOG_LOSS_EPSILON), 1.0 - LOG_LOSS_EPSILON)
    return -(outcome * math.log(clipped) + (1.0 - outcome) * math.log(1.0 - clipped))


async def fetch_resolutions(
    condition_ids: list[str],
    timeout: float = _RESOLUTION_TIMEOUT,
) -> dict[str, MarketResolution]:
    """Look up the settlement state per condition id on Gamma.

    Args:
        condition_ids: Market condition ids to resolve.
        timeout: Per-request HTTP timeout.

    Returns:
        ``{condition_id: MarketResolution}`` for every requested id. A
        resolved market carries its finalized winner and its close time;
        every other state carries the reason it was excluded. Callers must
        tolerate both.
    """
    unique_ids = []
    seen: set[str] = set()
    for cid in condition_ids or []:
        key = (cid or "").strip()
        if key and key not in seen:
            seen.add(key)
            unique_ids.append(key)
    if not unique_ids:
        return {}

    resolutions: dict[str, MarketResolution] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for start in range(0, len(unique_ids), _RESOLUTION_CONCURRENCY):
            chunk = unique_ids[start : start + _RESOLUTION_CONCURRENCY]
            found = await asyncio.gather(*(_lookup(client, cid) for cid in chunk))
            for item in found:
                if item is not None:
                    resolutions[item[0]] = item[1]
    return resolutions


async def _lookup(client, cid: str):
    """One Gamma lookup; ``(cid, MarketResolution)`` or ``None`` on failure."""
    try:
        response = await client.get(
            GAMMA_MARKETS_URL,
            params={"closed": "true", "condition_ids": cid},
        )
        if response.status_code != 200:
            logger.warning(
                "Gamma resolution lookup for %s returned %s",
                cid[:16],
                response.status_code,
            )
            return cid, MarketResolution(reason=ExclusionReason.LOOKUP_FAILED)
        markets = response.json()
    except Exception as exc:
        logger.warning("Gamma resolution lookup failed for %s: %s", cid[:16], exc)
        return cid, MarketResolution(reason=ExclusionReason.LOOKUP_FAILED)

    if not isinstance(markets, list) or not markets:
        return cid, MarketResolution(reason=ExclusionReason.MARKET_NOT_FOUND)
    matches = [
        item
        for item in markets
        if isinstance(item, dict)
        and str(item.get("conditionId") or item.get("condition_id") or "").lower() == cid.lower()
    ]
    if len(matches) != 1:
        # The answer names a different market (or several): the row can
        # never be matched to a resolution, and guessing would score it
        # against somebody else's outcome.
        return cid, MarketResolution(reason=ExclusionReason.CONDITION_ID_MISMATCH)
    return cid, classify_market(matches[0])


def _forecast_after_close(created_at: datetime | None, closed_at: datetime | None) -> bool:
    """True when the forecast was recorded after the market had closed."""
    if created_at is None or closed_at is None:
        return False
    created = created_at
    if created.tzinfo is None:
        # SQLite drops the offset on read; the stored value is UTC.
        created = created.replace(tzinfo=UTC)
    return created > closed_at


async def resolve_pending(db: Session, limit: int = _RESOLUTION_BATCH_LIMIT) -> ResolutionRun:
    """Resolve and Brier-score pending verdicts.

    Args:
        db: Active SQLAlchemy session.
        limit: Maximum number of pending verdicts per pass.

    Returns:
        A :class:`ResolutionRun` summary. Rows are only updated while
        ``resolved_at IS NULL``, so the pass is idempotent per market.
    """
    run = ResolutionRun()
    rows = AssessmentRepository(db).pending_resolutions(limit)
    if not rows:
        return run

    resolutions = await fetch_resolutions([row.condition_id for row in rows])
    for row in rows:
        run.checked += 1
        market = resolutions.get(row.condition_id or "")
        if market is None:
            market = MarketResolution(reason=ExclusionReason.MARKET_NOT_FOUND)
        if not market.is_resolved:
            reason = market.reason or ExclusionReason.CLOSED_NOT_FINALIZED
            run.exclude(row.condition_id, reason)
            row.exclusion_reason = reason.value
            continue
        if _forecast_after_close(row.created_at, market.closed_at):
            # The forecast was made after the market closed, so it could
            # already know the outcome: it is not a forecast any more.
            run.exclude(row.condition_id, ExclusionReason.FORECAST_AFTER_CLOSE)
            row.exclusion_reason = ExclusionReason.FORECAST_AFTER_CLOSE.value
            continue
        try:
            score = brier_score(row.ai_probability, market.outcome)
        except (ValueError, TypeError, OverflowError):
            # Caller can retry after corrected source or outcome mapping.
            run.exclude(row.condition_id, ExclusionReason.INVALID_PROBABILITY)
            row.exclusion_reason = ExclusionReason.INVALID_PROBABILITY.value
            continue
        row.resolved_outcome = market.outcome
        row.brier_score = score
        row.resolved_at = utc_now()
        row.scoring_version = SCORING_VERSION
        row.exclusion_reason = None
        run.resolved += 1
        run.brier_sum += score
        run.record_sample(row.condition_id, score)

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to persist forecast resolutions")
        raise
    global _last_run
    _last_run = run
    return run


def last_resolution_run() -> ResolutionRun:
    """The last resolution pass executed by this worker process.

    The pass summary is in-memory state: a deployment that runs the
    scheduler in a separate worker process sees the run of that worker
    only. The durable view of what was excluded lives on the rows
    themselves (``Assessment.exclusion_reason``) and is aggregated by the
    calibration summary.
    """
    return _last_run or ResolutionRun()


async def _resolution_loop():
    """Background loop: resolve pending verdicts on the configured interval.

    Shape copied from ``position_lifecycle_service._lifecycle_loop``:
    advisory lock first, then work → heartbeat → sleep → release, with
    every iteration error-isolated so one bad pass cannot kill the loop.
    """
    if not acquire_scheduler_lock(SCHEDULER_NAME):
        return
    interval = _resolution_interval()
    logger.info("Forecast resolution manager started (interval=%ds)", interval)

    try:
        while True:
            try:
                db = SessionLocal()
                try:
                    run = await resolve_pending(db)
                    if run.resolved or run.exclusions:
                        logger.info("Forecast resolution pass: %s", run.to_dict())
                finally:
                    db.close()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Forecast resolution loop error: %s", e)

            scheduler_heartbeat(SCHEDULER_NAME)
            await asyncio.sleep(interval)
    finally:
        release_scheduler_lock(SCHEDULER_NAME)

    logger.info("Forecast resolution manager stopped")


def _resolution_interval() -> int:
    """Effective loop interval in seconds (floored at 60s)."""
    try:
        return max(60, int(settings.forecast_resolution_interval_seconds))
    except (TypeError, ValueError):
        return 21600


async def start_forecast_resolution():
    """Start the forecast resolution background task."""
    global _resolution_task
    if _resolution_task and not _resolution_task.done():
        logger.warning("Forecast resolution manager already running")
        return
    _resolution_task = asyncio.create_task(_resolution_loop())
    logger.info("Forecast resolution manager started")


async def stop_forecast_resolution():
    """Stop the forecast resolution background task."""
    global _resolution_task
    if _resolution_task and not _resolution_task.done():
        _resolution_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _resolution_task
    _resolution_task = None
    logger.info("Forecast resolution manager stopped")
