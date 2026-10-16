"""Backtest and historical trade queries."""

from sqlalchemy.orm import Session

from app.models.analytics.backtest_run import BacktestRun
from app.models.trading.user_trade import UserTrade


class BacktestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def historical_trades(self, wallet: str, start_date: str, end_date: str) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.copied_from_wallet == wallet,
                UserTrade.executed_at >= start_date,
                UserTrade.executed_at <= end_date,
            )
            .order_by(UserTrade.executed_at.asc())
            .all()
        )

    def get_run(self, run_id: int) -> BacktestRun | None:
        return self.db.query(BacktestRun).filter(BacktestRun.id == run_id).first()

    def list_runs(self, user_id: int, limit: int) -> list[BacktestRun]:
        return (
            self.db.query(BacktestRun)
            .filter(BacktestRun.user_id == user_id)
            .order_by(BacktestRun.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_user_run(self, run_id: int, user_id: int) -> BacktestRun | None:
        return (
            self.db.query(BacktestRun)
            .filter(BacktestRun.id == run_id, BacktestRun.user_id == user_id)
            .first()
        )
