"""Stateless, authenticated research endpoints for copy-trader evaluation (#158).

Nothing here reads wallets, signs, or places orders. Inputs are caller-supplied
and bounded; outputs are modeled research, never exchange fills.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.analytics.follower_lag import (
    LagSettings,
    LeaderTrade,
    copy_skip_decision,
    lag_report,
)
from app.services.analytics.replay.follower import Book
from app.services.analytics.trader_scoring import ResolvedPosition, score_resolved_positions
from app.services.analytics.trader_walk_forward import TraderHistory, walk_forward

router = APIRouter(prefix="/api/trades/traders", tags=["trader evaluation"])

Delays = Annotated[list[Annotated[int, Field(ge=0, le=86400)]], Field(min_length=1, max_length=6)]


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positions: list[ResolvedPosition] = Field(max_length=5000)


class LagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trades: list[LeaderTrade] = Field(max_length=2000)
    books: list[Book] = Field(max_length=2000)
    delays_seconds: Delays = [0, 60, 300]
    settings: LagSettings = LagSettings()


class SkipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    side: str = Field(pattern="^(BUY|SELL)$")
    leader_price: Decimal = Field(gt=0, le=1)
    current_price: Decimal = Field(gt=0, le=1)
    edge: Decimal = Field(ge=0, le=1)
    max_edge_fraction: Decimal | None = Field(default=None, ge=0, le=10)


class WalkForwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    histories: list[TraderHistory] = Field(min_length=2, max_length=200)
    books: list[Book] = Field(max_length=5000)
    cutoffs: list[AwareDatetime] = Field(min_length=1, max_length=12)
    end_at: AwareDatetime
    delay_seconds: int = Field(default=60, ge=0, le=86400)
    settings: LagSettings = LagSettings()
    quantile: float = Field(default=0.25, gt=0, le=0.5)


@router.post("/score")
def score(body: ScoreRequest, current_user: dict = Depends(get_current_user_from_token)):
    del current_user
    return score_resolved_positions(body.positions)


@router.post("/follower-lag")
def follower_lag(body: LagRequest, current_user: dict = Depends(get_current_user_from_token)):
    del current_user
    return lag_report(body.trades, body.books, body.delays_seconds, body.settings)


@router.post("/copy-skip")
def copy_skip(body: SkipRequest, current_user: dict = Depends(get_current_user_from_token)):
    """Evaluate the configurable skip rule; the default fraction is a deployment setting."""
    del current_user
    fraction = body.max_edge_fraction
    if fraction is None:
        fraction = Decimal(str(get_settings().copy_skip_edge_fraction))
    return {
        "max_edge_fraction": str(fraction),
        **copy_skip_decision(
            side=body.side,
            leader_price=body.leader_price,
            current_price=body.current_price,
            edge=body.edge,
            max_edge_fraction=fraction,
        ),
    }


@router.post("/walk-forward")
def walk_forward_report(
    body: WalkForwardRequest, current_user: dict = Depends(get_current_user_from_token)
):
    del current_user
    try:
        return walk_forward(
            body.histories,
            body.books,
            cutoffs=body.cutoffs,
            end_at=body.end_at,
            delay_seconds=body.delay_seconds,
            settings=body.settings,
            quantile=body.quantile,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
