"""Trade-history queries used by analytics and simulation services."""

from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account


class AnalyticsTradeRepository:
    def __init__(self, db: Session, account_id: int | None = None) -> None:
        self.db = db
        self.account_id = account_id

    def _scope(self, query, user_id):
        if self.account_id is None:
            return query
        account = self.db.get(Account, self.account_id)
        if account is None or account.owner_user_id != user_id:
            raise ValueError("Account unavailable")
        clause = UserTrade.account_id == account.id
        if account.kind == "personal":
            clause = or_(clause, UserTrade.account_id.is_(None))
        return query.filter(clause)

    def list_strategy_trades(self, user_id: int, cutoff: datetime) -> list[UserTrade]:
        return self._scope(
            self.db.query(UserTrade).filter(
                UserTrade.user_id == user_id,
                UserTrade.created_at >= cutoff,
                UserTrade.strategy_source.isnot(None),
            ),
            user_id,
        ).all()

    def count_unattributed_trades(self, user_id: int, cutoff: datetime) -> int:
        return int(
            self._scope(
                self.db.query(UserTrade.id).filter(
                    UserTrade.user_id == user_id,
                    UserTrade.created_at >= cutoff,
                    UserTrade.strategy_source.is_(None),
                ),
                user_id,
            ).count()
        )

    def list_cohort_trades(self, user_id: int, cutoff: datetime) -> list[UserTrade]:
        return self._scope(
            self.db.query(UserTrade).filter(
                UserTrade.user_id == user_id, UserTrade.created_at >= cutoff
            ),
            user_id,
        ).all()

    def list_trade_details(self, user_id: int, strategy: str | None, limit: int) -> list[UserTrade]:
        query = self.db.query(UserTrade).filter(UserTrade.user_id == user_id)
        if strategy:
            query = query.filter(UserTrade.strategy_source == strategy)
        return (
            self._scope(query, user_id)
            .order_by(UserTrade.created_at.desc(), UserTrade.id.desc())
            .limit(limit)
            .all()
        )

    def reconciliation_trades(self, user_id: int) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.action.in_(("buy", "sell")),
                UserTrade.executed_at.isnot(None),
            )
            .order_by(UserTrade.executed_at.asc(), UserTrade.id.asc())
            .all()
        )

    def total_realized_pnl(self, user_id: int) -> float:
        value = (
            self.db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
            .filter(UserTrade.user_id == user_id, UserTrade.pnl.isnot(None))
            .scalar()
        )
        return float(value or 0.0)

    def has_pnl_history(self, user_id: int) -> bool:
        return (
            self.db.query(UserTrade.id)
            .filter(UserTrade.user_id == user_id, UserTrade.pnl.isnot(None))
            .limit(1)
            .count()
            > 0
        )

    def unreconciled_sell_count(self, user_id: int, statuses: tuple[str, ...]) -> int:
        return int(
            self.db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.action == "sell",
                UserTrade.status.in_(statuses),
                UserTrade.pnl.is_(None),
            )
            .count()
        )

    def simulated_trades(self, user_id: int) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(UserTrade.user_id == user_id, UserTrade.status == "simulated")
            .all()
        )

    def entry_trades(self, user_id: int, token_id: str) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.token_id == token_id,
                UserTrade.action == "buy",
                UserTrade.status.in_(("executed", "simulated")),
            )
            .all()
        )
