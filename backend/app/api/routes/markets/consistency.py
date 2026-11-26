"""Read-only cross-market consistency alerts; no order, signing or wallet access."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.analytics.consistency.cross_market_consistency import (
    ConsistencyRequest,
    ConsistencyResult,
    analyze,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/v1/consistency",
    tags=["cross-market consistency"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().cross_market_consistency_enabled:
        raise HTTPException(503, "Cross-market consistency analysis disabled")


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return {
        "input_schema": ConsistencyRequest.model_json_schema(),
        "output_schema": ConsistencyResult.model_json_schema(),
    }


@router.post("/analyze", dependencies=[Depends(enabled)])
def analyze_relation(payload: ConsistencyRequest) -> ConsistencyResult:
    result = analyze(payload)
    logger.info("consistency_analyzed", extra={"telemetry": result.telemetry})
    return result
