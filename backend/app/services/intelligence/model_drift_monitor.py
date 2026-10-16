"""Model-drift detection scheduler (plan 04).

An hourly, advisory-locked loop that scores the calibration window and
dispatches a ``model_drift`` alert for every drifting model/task. The
alert is admin-scoped: it is an operational signal about the product's
forecasting quality, not something every user should be woken for.

The dead-man's-switch watchdog already covers "the heartbeat went
stale"; this job covers the other failure mode — a scheduler that is
running fine while the models it calls are quietly getting worse.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.services.intelligence.model_drift import (
    DEFAULT_WINDOW_DAYS,
    detect_drift,
)
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)

logger = logging.getLogger(__name__)

DRIFT_CHECK_INTERVAL = 3600  # hourly
SCHEDULER_NAME = "model_drift"

_drift_task: asyncio.Task | None = None
_last_report: dict | None = None
_metrics = {
    "checks": 0,
    "drift_alerts_sent": 0,
    "errors": 0,
}


def _metric_inc(key: str) -> None:
    _metrics[key] = _metrics.get(key, 0) + 1


def get_drift_metrics() -> dict:
    """Counters for the watchdog and the calibration panel."""
    return dict(_metrics)


def get_last_drift_report() -> dict | None:
    """Most recent detection result (kept small: the drifted rows only)."""
    return _last_report


async def _drift_check_once() -> None:
    """One scoring pass: detect, alert, record."""
    global _last_report
    db = SessionLocal()
    try:
        report = detect_drift(db, window_days=DEFAULT_WINDOW_DAYS)
    finally:
        db.close()

    _metric_inc("checks")
    _last_report = {
        "window_days": report["window_days"],
        "drifted_count": report["drifted_count"],
        "drifted": report["drifted"],
    }
    scheduler_heartbeat(SCHEDULER_NAME)

    if not report["drifted"]:
        return

    try:
        from app.services.users.alert_service import dispatch

        for row in report["drifted"]:
            await dispatch(
                "model_drift",
                None,
                {
                    "model_name": row.get("model_name") or "unknown",
                    "task": row.get("task") or "unknown",
                    "samples": row.get("samples"),
                    "mean_brier": row.get("mean_brier"),
                    "baseline_brier": row.get("baseline_brier"),
                    "excess_over_baseline": row.get("excess_over_baseline"),
                    "slope_per_day": row.get("slope_per_day"),
                    "window_days": row.get("window_days"),
                    "reasons": row.get("reasons"),
                },
            )
            _metric_inc("drift_alerts_sent")
    except Exception:
        _metric_inc("errors")
        logger.warning("Model-drift alert dispatch failed", exc_info=True)


async def _drift_loop() -> None:
    while True:
        try:
            await _drift_check_once()
        except Exception:
            _metric_inc("errors")
            logger.warning("Model-drift check failed", exc_info=True)
        await asyncio.sleep(DRIFT_CHECK_INTERVAL)


async def start_model_drift_monitor() -> None:
    """Start the hourly drift detector (skips when the lock is held)."""
    global _drift_task
    if _drift_task is not None and not _drift_task.done():
        logger.info("Model-drift monitor already running")
        return
    if not acquire_scheduler_lock(SCHEDULER_NAME):
        return
    _drift_task = asyncio.create_task(_drift_loop())
    logger.info("Model-drift monitor started (interval=%ds)", DRIFT_CHECK_INTERVAL)


async def stop_model_drift_monitor() -> None:
    """Stop the drift detector and release its advisory lock."""
    global _drift_task, _last_report
    if _drift_task is not None and not _drift_task.done():
        _drift_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _drift_task
    _drift_task = None
    _last_report = None
    release_scheduler_lock(SCHEDULER_NAME)
    logger.info("Model-drift monitor stopped")
