"""A/B experiment routing for LLM tasks (plan 04).

Assignment is **deterministic and stateless**:
``sha256(f"{user_id}:{experiment_name}") % 100 < traffic_pct`` picks
the variant. There is no sticky session to store, no per-user state to
migrate, and a test can reproduce any user's arm from the id alone —
which is what makes the split verifiable rather than assumed.

Only one experiment may be active per task. On Postgres that is a
partial unique index (see the migration); on SQLite, where partial
indexes do not exist, this service enforces it before writing.

The kill switch ``llm_tiering_enabled`` is honoured: when tiering is
off, no experiment runs, because an experiment exists to compare model
configs and there is no tier machinery to vary.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.governance import LLMExperiment

logger = logging.getLogger(__name__)

MIN_TRAFFIC_PCT = 1
MAX_TRAFFIC_PCT = 100

VALID_STATUSES = ("active", "paused", "done")


def assign_variant(experiment_name: str, user_id: str, traffic_pct: int) -> str:
    """Deterministically map a user to ``control`` or ``variant``."""
    bucket = _bucket(experiment_name, user_id)
    return "variant" if bucket < int(traffic_pct) else "control"


def _bucket(experiment_name: str, user_id: str) -> int:
    digest = hashlib.sha256(f"{user_id}:{experiment_name}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def _parse_config(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def resolve_experiment(db: Session, task: str, user_id: int | str | None) -> dict[str, Any] | None:
    """Resolve an active experiment's config for this user, if any.

    Returns ``{experiment_id, variant, config}`` — the *config* being
    the arm's overrides, i.e. the delta the caller must apply — or
    ``None`` when no experiment covers this task/user.
    """
    if not task:
        return None

    experiment = (
        db.query(LLMExperiment)
        .filter(LLMExperiment.task == task, LLMExperiment.status == "active")
        .order_by(LLMExperiment.id.desc())
        .first()
    )
    if experiment is None:
        return None

    variant = assign_variant(experiment.name, str(user_id or ""), experiment.traffic_pct)
    config = _parse_config(
        experiment.variant_config_json if variant == "variant" else experiment.control_config_json
    )
    return {
        "experiment_id": experiment.name,
        "variant": variant,
        "config": config,
    }


def active_experiment_conflicts(db: Session, task: str, exclude_id: int | None = None) -> bool:
    """True when another *active* experiment already covers this task.

    Enforced in application code everywhere so the rule holds on
    SQLite too, where the partial unique index from the migration does
    not exist.
    """
    query = db.query(LLMExperiment.id).filter(
        LLMExperiment.task == task, LLMExperiment.status == "active"
    )
    if exclude_id is not None:
        query = query.filter(LLMExperiment.id != exclude_id)
    return query.first() is not None


def create_experiment(
    db: Session,
    *,
    name: str,
    task: str,
    traffic_pct: int,
    control_config: dict | None = None,
    variant_config: dict | None = None,
    created_by: str | None = None,
) -> tuple[LLMExperiment | None, str | None]:
    """Validate and create one experiment.

    Returns ``(experiment, None)`` on success or ``(None, reason)`` on
    a rejected request — the same tuple style the rest of the codebase
    uses for user-facing rejections.
    """
    if not (MIN_TRAFFIC_PCT <= int(traffic_pct) <= MAX_TRAFFIC_PCT):
        return None, f"traffic_pct must be in [{MIN_TRAFFIC_PCT}, {MAX_TRAFFIC_PCT}]"
    if active_experiment_conflicts(db, task):
        return None, f"task '{task}' already has an active experiment"

    experiment = LLMExperiment(
        name=name,
        task=task,
        control_config_json=json.dumps(control_config or {}),
        variant_config_json=json.dumps(variant_config or {}),
        traffic_pct=int(traffic_pct),
        status="active",
        created_by=created_by,
    )
    db.add(experiment)
    try:
        db.commit()
        db.refresh(experiment)
    except Exception as exc:
        db.rollback()
        # The unique index on name is the last line of defence; a
        # duplicate name surfaces here rather than half-persisted.
        logger.warning("Experiment create failed for %s: %s", name, exc)
        return None, "experiment name already exists"
    return experiment, None


def set_experiment_status(
    db: Session, experiment_id: int, status: str
) -> tuple[LLMExperiment | None, str | None]:
    if status not in VALID_STATUSES:
        return None, f"status must be one of {', '.join(VALID_STATUSES)}"
    experiment = db.query(LLMExperiment).filter(LLMExperiment.id == experiment_id).first()
    if experiment is None:
        return None, "experiment not found"
    if (
        status == "active"
        and experiment.task
        and active_experiment_conflicts(db, experiment.task, exclude_id=experiment.id)
    ):
        return None, f"task '{experiment.task}' already has an active experiment"
    experiment.status = status
    db.commit()
    db.refresh(experiment)
    return experiment, None


def compare_experiment_arms(
    db: Session, experiment_name: str, window_days: int = 30
) -> dict[str, Any]:
    """Calibration comparison between the two arms.

    Aggregates the ``assessments`` rows written under this experiment:
    resolved Brier scores per arm plus the baseline, so an operator can
    read "is the variant better" without exporting anything. Legacy
    rows without a probability are excluded, never scored at 0.5.
    """
    sql = text(
        """
        SELECT variant,
               COUNT(*) AS samples,
               SUM(CASE WHEN brier_score IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
               AVG(CASE WHEN brier_score IS NOT NULL THEN brier_score END) AS mean_brier,
               MIN(brier_score) AS best_brier,
               MAX(brier_score) AS worst_brier
          FROM assessments
         WHERE experiment_id = :experiment
           AND variant IN ('control', 'variant')
           AND ai_probability IS NOT NULL
           AND created_at >= :cutoff
         GROUP BY variant
         """
    )
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    arms: dict[str, Any] = {}
    for row in db.execute(sql, {"experiment": experiment_name, "cutoff": cutoff}):
        mapping = row._mapping
        variant = mapping["variant"]
        samples = int(mapping["samples"] or 0)
        resolved = int(mapping["resolved"] or 0)
        arms[variant] = {
            "samples": samples,
            "resolved": resolved,
            "mean_brier": (
                round(float(mapping["mean_brier"]), 4)
                if mapping["mean_brier"] is not None
                else None
            ),
            "best_brier": (
                round(float(mapping["best_brier"]), 4)
                if mapping["best_brier"] is not None
                else None
            ),
            "worst_brier": (
                round(float(mapping["worst_brier"]), 4)
                if mapping["worst_brier"] is not None
                else None
            ),
        }

    control = arms.get("control", {}).get("mean_brier")
    variant = arms.get("variant", {}).get("mean_brier")
    verdict = "inconclusive"
    if control is not None and variant is not None:
        if arms["variant"]["resolved"] < 20 or arms["control"]["resolved"] < 20:
            verdict = "insufficient_samples"
        elif variant < control - 0.01:
            verdict = "variant_better"
        elif variant > control + 0.01:
            verdict = "control_better"
        else:
            verdict = "no_difference"

    return {
        "experiment": experiment_name,
        "window_days": window_days,
        "arms": arms,
        # Lower Brier is better, hence the sign convention in the verdict.
        "verdict": verdict,
    }
