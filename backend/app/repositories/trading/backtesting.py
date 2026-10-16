"""Repository reads for a user's backtest runs."""

from __future__ import annotations

from app.models.analytics.backtest_run import BacktestRun
from app.repositories.base import SQLAlchemyRepository


class TradingBacktestRepository(SQLAlchemyRepository[BacktestRun]):
    model = BacktestRun

    def find_run_for_user(self, run_id: int, user_id: int) -> BacktestRun | None:
        return (
            self.db.query(BacktestRun)
            .filter(BacktestRun.id == run_id, BacktestRun.user_id == user_id)
            .first()
        )
