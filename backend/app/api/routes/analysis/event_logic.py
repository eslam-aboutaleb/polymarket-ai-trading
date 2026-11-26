"""Read-only event-logic skill (#20): coherence bounds between related contracts.

Advisory research only. It never reaches ``pre_trade_gate``, ``live_trading_policy``, the
order gateway or any wallet capability, and responses always say ``execution_eligible: false``.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.intelligence import event_logic_skill as skill

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/analysis/event-logic",
    tags=["event logic"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().event_logic_enabled:
        raise HTTPException(503, "Event logic skill disabled")


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return skill.describe()


@router.post("/analyze", dependencies=[Depends(enabled)])
def analyze(payload: skill.EventLogicRequest) -> dict[str, Any]:
    started = time.perf_counter()
    result = skill.analyze(payload)
    logger.info(
        "event_logic status=%s reason=%s events=%d relations=%d elapsed_ms=%d",
        result["status"],
        result.get("reason"),
        len(payload.events),
        len(payload.relations),
        int((time.perf_counter() - started) * 1000),
    )
    return result
