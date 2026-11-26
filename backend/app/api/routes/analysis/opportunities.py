"""Read-only executable-opportunity ranking. Never places, signs or cancels an order."""

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.microstructure import owned_account
from app.config import get_settings
from app.models import Account
from app.models.analytics.assessment import Assessment
from app.security.rate_limit import limiter
from app.services.analytics.execution_quality import load_book
from app.services.opportunities.opportunity_engine import (
    CandidateRequest,
    ForecastInput,
    evaluate_opportunity,
    rank_opportunities,
)
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(
    prefix="/api/opportunities",
    tags=["executable opportunities (read-only)"],
    dependencies=[Depends(get_current_user_from_token)],
)
logger = logging.getLogger(__name__)
MAX_CANDIDATES = 25


def enabled():
    if not get_settings().executable_opportunities_enabled:
        raise HTTPException(503, "Executable opportunities disabled")


def _evaluate_both(db, forecast, shares, fee_bps, fee_version, max_age, now):
    out = []
    for outcome in ("YES", "NO"):
        spec, book, reason = load_book(db, forecast.condition_id, outcome, now)
        if spec is None:
            continue
        out.append(
            evaluate_opportunity(
                spec,
                book,
                forecast,
                CandidateRequest(
                    outcome=outcome,
                    shares=shares,
                    fee_bps=fee_bps,
                    fee_version=fee_version,
                    max_forecast_age_seconds=max_age,
                ),
                now,
            )
        )
    return out


def _payload(opportunities, min_edge):
    ranked, filtered = rank_opportunities(opportunities, min_edge)
    return {
        "version": "opportunity-v1",
        "mode": "research_only_no_order",
        "ranked": [o.model_dump(mode="json") for o in ranked],
        "filtered": [
            {"reason": reason, "opportunity": o.model_dump(mode="json")} for o, reason in filtered
        ],
        "guaranteed_profit": False,
        "notice": "Indicative or verified edges are estimates from displayed depth, not profit.",
    }


@router.get("", dependencies=[Depends(enabled)])
@limiter.limit("30/minute")
def list_opportunities(
    request: Request,
    strategy: Literal["ai_forecast", "copy_signal", "arbitrage"] = "ai_forecast",
    min_edge: Decimal | None = Query(None, ge=0, le=10),
    max_age: int = Query(3600, ge=1, le=86400),
    shares: Decimal = Query(Decimal("10"), gt=0, le=1_000_000, decimal_places=8),
    fee_bps: Decimal | None = Query(None, ge=0, le=10000),
    fee_version: str | None = Query(None, min_length=1, max_length=80),
    _account: Account = Depends(owned_account),
    db: Session = Depends(get_db),
):
    now = utc_now()
    if strategy != "ai_forecast":
        return {**_payload([], min_edge), "note": f"No {strategy} candidate source is wired yet."}
    rows = (
        db.query(Assessment)
        .filter(
            Assessment.condition_id.isnot(None),
            Assessment.ai_probability.isnot(None),
            Assessment.created_at >= now - timedelta(seconds=max_age),
        )
        .order_by(Assessment.created_at.desc(), Assessment.id.desc())
        .limit(500)
        .all()
    )
    latest = {}
    for row in rows:
        latest.setdefault(row.condition_id.lower(), row)
    opportunities = []
    for condition, row in list(latest.items())[:MAX_CANDIDATES]:
        # Stored verdicts carry neither an interval, a calibration status nor a rule hash,
        # so these candidates can be INDICATIVE at best and are labelled as such.
        forecast = ForecastInput(
            forecast_id=f"assessment-{row.id}",
            condition_id=condition,
            yes_probability=Decimal(str(row.ai_probability)),
            forecast_at=row.created_at
            if row.created_at.tzinfo
            else row.created_at.replace(tzinfo=now.tzinfo),
        )
        try:
            opportunities += _evaluate_both(
                db, forecast, shares, fee_bps, fee_version, max_age, now
            )
        except (ValueError, ArithmeticError):
            logger.warning("Opportunity candidate failed validation")
    return _payload(opportunities, min_edge)


class EvaluateBody(BaseModel):
    forecast: ForecastInput
    shares: Decimal = Field(gt=0, le=1_000_000, decimal_places=8)
    fee_bps: Decimal | None = Field(default=None, ge=0, le=10000)
    fee_version: str | None = Field(default=None, min_length=1, max_length=80)
    max_forecast_age_seconds: int = Field(default=3600, ge=1, le=86400)


@router.post("/evaluate", dependencies=[Depends(enabled)])
@limiter.limit("30/minute")
def evaluate(
    request: Request,
    body: EvaluateBody,
    _account: Account = Depends(owned_account),
    db: Session = Depends(get_db),
):
    """Evaluate one explicit forecast (with interval, calibration, rule hash) at a size."""
    try:
        items = _evaluate_both(
            db,
            body.forecast,
            body.shares,
            body.fee_bps,
            body.fee_version,
            body.max_forecast_age_seconds,
            utc_now(),
        )
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(409, "Captured book evidence failed validation") from exc
    return _payload(items, None)
