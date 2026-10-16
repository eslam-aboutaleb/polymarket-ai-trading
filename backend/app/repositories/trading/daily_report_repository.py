"""Trade and protective-order reads for portfolio digests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.trading.stop_loss import StopLossOrder
from app.models.trading.user_trade import UserTrade


class TradingReportRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def realized_pnl_since(self, user_id: int, since: datetime, *, paper: bool) -> float:
        timestamp = UserTrade.created_at if paper else UserTrade.executed_at
        stmt = select(func.coalesce(func.sum(UserTrade.pnl), 0.0)).where(
            UserTrade.user_id == user_id,
            timestamp >= since,
        )
        if paper:
            stmt = stmt.where(UserTrade.status == "simulated")
        return float(self.db.execute(stmt).scalar_one() or 0.0)

    def triggered_stop_losses_since(
        self, user_id: int, since: datetime, limit: int
    ) -> list[StopLossOrder]:
        stmt = (
            select(StopLossOrder)
            .where(
                StopLossOrder.user_id == user_id,
                StopLossOrder.triggered_at >= since,
            )
            .order_by(StopLossOrder.triggered_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())
