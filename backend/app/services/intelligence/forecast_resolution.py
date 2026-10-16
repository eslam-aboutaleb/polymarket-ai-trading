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
* The winning outcome is the decoded pair member whose price is
  ``>= 0.5``: ``["1","0"]`` → YES wins, ``["0","1"]`` → NO wins.
  ``umaResolutionStatus`` (``"resolved"``) is present on settled
  markets but the price pair is the authoritative signal; markets
  where no outcome reaches the threshold are skipped.
* ``closed=true`` restricts the lookup to settled markets.

Brier convention: binary YES framing. ``ai_probability`` is the AI's
probability that YES resolves true, so the outcome is ``1.0`` when the
winning label is YES-like (``"yes"``) and ``0.0`` otherwise. The raw
winning label is preserved in ``resolved_outcome`` (normalised to
upper case) for transparency, including the rare non-binary label.

Idempotency: ``resolve_pending`` only touches rows where
``resolved_at IS NULL``, so a re-run (the advisory lock fails open on
database errors, so exclusivity is never assumed) scores each market
at most once.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
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

# Yes-like labels map to outcome 1.0; anything else is "YES did not
# happen" (0.0) under the binary framing documented above.
_YES_LABELS = {"yes", "y", "true", "1"}

_resolution_task: asyncio.Task | None = None


@dataclass
class ResolutionRun:
    """Summary of one resolution pass."""

    checked: int = 0
    resolved: int = 0
    brier_sum: float = 0.0
    unresolved_condition_ids: list[str] = field(default_factory=list)

    @property
    def mean_brier(self) -> float | None:
        return self.brier_sum / self.resolved if self.resolved else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "resolved": self.resolved,
            "mean_brier": None if self.mean_brier is None else round(self.mean_brier, 4),
            "unresolved": len(self.unresolved_condition_ids),
        }


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


def winning_outcome(market: dict[str, Any]) -> str | None:
    """Return the upper-cased winning label of a Gamma market payload.

    ``outcomes``/``outcomePrices`` are JSON-encoded strings; the winner
    is the outcome whose price reaches 0.5. Returns ``None`` when the
    payload is unusable or no outcome has resolved (both prices 0, or a
    malformed pair).
    """
    outcomes = _decode_json_string(market.get("outcomes"))
    prices = _decode_json_string(market.get("outcomePrices"))
    if len(outcomes) != len(prices) or not outcomes:
        return None
    for label, raw_price in zip(outcomes, prices, strict=False):
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price >= 0.5:
            return str(label).strip().upper()[:20]
    return None


def outcome_value(resolved_outcome: str | None) -> float:
    """Map a resolved label to its YES-framing outcome (1.0 YES, else 0.0)."""
    if resolved_outcome and resolved_outcome.strip().lower() in _YES_LABELS:
        return 1.0
    return 0.0


def brier_score(ai_probability: float, resolved_outcome: str | None) -> float:
    """Brier score ``(p_yes - outcome)^2`` for one resolved verdict."""
    error = float(ai_probability) - outcome_value(resolved_outcome)
    return error * error


async def fetch_resolutions(
    condition_ids: list[str],
    timeout: float = _RESOLUTION_TIMEOUT,
) -> dict[str, str]:
    """Look up the winning outcome per condition id on Gamma.

    Args:
        condition_ids: Market condition ids to resolve.
        timeout: Per-request HTTP timeout.

    Returns:
        ``{condition_id: "YES"|"NO"|<label>}`` for every market that
        has a determinable winner. Ids that are missing, unresolved or
        unreachable are simply absent — callers must tolerate gaps.
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

    resolutions: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for start in range(0, len(unique_ids), _RESOLUTION_CONCURRENCY):
            chunk = unique_ids[start : start + _RESOLUTION_CONCURRENCY]
            found = await asyncio.gather(*(_lookup(client, cid) for cid in chunk))
            for item in found:
                if item is not None:
                    resolutions[item[0]] = item[1]
    return resolutions


async def _lookup(client, cid: str):
    """One Gamma lookup; ``(cid, "YES"|"NO")`` or ``None`` on any failure."""
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
            return None
        markets = response.json()
    except Exception as exc:
        logger.warning("Gamma resolution lookup failed for %s: %s", cid[:16], exc)
        return None

    if not isinstance(markets, list) or not markets:
        return None
    winner = winning_outcome(markets[0])
    if winner is None:
        return None
    return cid, winner


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
        winner = resolutions.get(row.condition_id or "")
        if winner is None:
            run.unresolved_condition_ids.append(row.condition_id or "")
            continue
        row.resolved_outcome = winner
        score = brier_score(row.ai_probability, winner)
        row.brier_score = score
        row.resolved_at = utc_now()
        run.resolved += 1
        run.brier_sum += score

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to persist forecast resolutions")
        raise
    return run


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
                    if run.resolved:
                        logger.info(
                            "Forecast resolution pass: %d/%d scored (mean Brier %s)",
                            run.resolved,
                            run.checked,
                            run.mean_brier,
                        )
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
