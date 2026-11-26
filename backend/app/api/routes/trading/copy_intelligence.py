"""Authenticated, stateless copy-trading evaluation and shadow endpoints (issue #6).

Paper and shadow only: no wallet, signing or order code is reachable from here.
"""

import logging
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.api.routes.auth.auth import get_current_user_from_token
from app.services.analytics.replay.follower import Book
from app.services.trading.copy_trade_intelligence import (
    challenger_report,
    independent_votes,
    trader_profile,
)
from app.services.trading.follower_execution_simulator import PairedLeaderTrade, follow_trade

router = APIRouter(prefix="/api/v1/copy-trading", tags=["copy-trading intelligence"])
logger = logging.getLogger(__name__)
Money = Annotated[Decimal, Field(ge=0, le=1_000_000, max_digits=18, decimal_places=8)]


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    market_id: str = Field(min_length=1, max_length=200)
    cost: Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=8)]
    pnl: Annotated[Decimal, Field(max_digits=24, decimal_places=8)]


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trade: PairedLeaderTrade
    books: list[Book] = Field(max_length=250)
    delays_seconds: list[Annotated[int, Field(ge=0, le=86400)]] = Field(
        default=[0, 60, 300], min_length=1, max_length=6
    )
    notional: Annotated[Decimal, Field(gt=0, le=1_000_000, max_digits=18, decimal_places=8)] = (
        Decimal(10)
    )
    fee_bps: Annotated[Decimal, Field(ge=0, le=10000)] = Decimal(0)
    settlement_payout: Annotated[Decimal, Field(ge=0, le=1)] | None = None
    settlement_at: AwareDatetime | None = None
    trader_positions: list[Position] = Field(default_factory=list, max_length=5000)
    baseline_return: float = Field(default=0.0, ge=-1, le=1)


class VoteSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wallet: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    market_id: str = Field(min_length=1, max_length=200)
    side: Literal["BUY", "SELL"]


class Cluster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_id: str = Field(min_length=1, max_length=60)
    wallets: list[str] = Field(min_length=1, max_length=200)


class ShadowRequest(EvaluateRequest):
    signals: list[VoteSignal] = Field(default_factory=list, max_length=500)
    clusters: list[Cluster] = Field(default_factory=list, max_length=100)


class ChallengerRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wallet: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    market_id: str = Field(min_length=1, max_length=200)
    first_seen_at: AwareDatetime
    cost: Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=8)]
    pnl: Annotated[Decimal, Field(max_digits=24, decimal_places=8)]
    independent_votes: int = Field(default=1, ge=0, le=1000)


class ChallengerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[ChallengerRow] = Field(max_length=20000)
    split_at: AwareDatetime
    min_votes: int = Field(default=1, ge=1, le=20)


def _paired(body: EvaluateRequest) -> dict:
    try:
        results = [
            follow_trade(
                body.trade,
                body.books,
                delay_seconds=delay,
                notional=body.notional,
                fee_bps=body.fee_bps,
                settlement_payout=body.settlement_payout,
                settlement_at=body.settlement_at,
            )
            for delay in body.delays_seconds
        ]
    except ValueError as exc:
        raise HTTPException(422, "Inconsistent replay inputs") from exc
    profile = trader_profile(
        body.trade.wallet,
        body.trade.category,
        [p.model_dump() for p in body.trader_positions],
        baseline_return=body.baseline_return,
    )
    return {
        "research_only": True,
        "execution_eligible": False,
        "paired_trades": results,
        "trader_profile": profile,
    }


@router.post("/evaluate")
def evaluate(body: EvaluateRequest, current_user: dict = Depends(get_current_user_from_token)):
    del current_user
    return _paired(body)


@router.post("/shadow")
def shadow(body: ShadowRequest, current_user: dict = Depends(get_current_user_from_token)):
    """Shadow copy: what would have happened, with independent votes. Places no order."""
    result = _paired(body)
    votes = independent_votes(
        [s.model_dump() for s in body.signals], [c.model_dump() for c in body.clusters]
    )
    key = (body.trade.market_id, "BUY")
    result.update(
        votes=votes.get(key, {"raw_votes": 0, "independent_votes": 0}),
        orders_placed=False,
        shadow=True,
    )
    logger.info(
        "Copy shadow evaluated user=%s trade=%s", current_user.get("user_id"), body.trade.trade_id
    )
    return result


@router.post("/challenger")
def challenger(body: ChallengerRequest, current_user: dict = Depends(get_current_user_from_token)):
    del current_user
    return challenger_report(
        [r.model_dump() for r in body.rows], split_at=body.split_at, min_votes=body.min_votes
    )
