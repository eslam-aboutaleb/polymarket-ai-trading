"""Repository operations for copy-trading follows."""

from __future__ import annotations

from app.models.trading.followed_trader import FollowedTrader
from app.repositories.base import SQLAlchemyRepository


class TradingFollowRepository(SQLAlchemyRepository[FollowedTrader]):
    model = FollowedTrader

    def list_active_wallets_for_user(self, user_id: int) -> list[str]:
        rows = (
            self.db.query(FollowedTrader.trader_wallet)
            .filter(FollowedTrader.user_id == user_id, FollowedTrader.is_active)
            .all()
        )
        return [row.trader_wallet for row in rows]

    def list_active_wallets(self, user_id: int | None = None) -> list[str]:
        if user_id is not None:
            return self.list_active_wallets_for_user(user_id)
        rows = (
            self.db.query(FollowedTrader.trader_wallet)
            .filter(FollowedTrader.is_active)
            .distinct()
            .all()
        )
        wallets = []
        for row in rows:
            w = row[0] if isinstance(row, (tuple, list)) else getattr(row, "trader_wallet", None)
            if w:
                wallets.append(w)
        return wallets

    def has_active_watcher(self, wallet: str) -> bool:
        return (
            self.db.query(FollowedTrader)
            .filter(FollowedTrader.trader_wallet == wallet, FollowedTrader.is_active)
            .first()
            is not None
        )

    def copy_watcher_exists(self, wallet: str) -> bool:
        return self.has_active_watcher(wallet)

    def find_follow(self, user_id: int, wallet: str) -> FollowedTrader | None:
        return (
            self.db.query(FollowedTrader)
            .filter(FollowedTrader.user_id == user_id, FollowedTrader.trader_wallet == wallet)
            .first()
        )

    def list_active_follows(self, user_id: int) -> list[FollowedTrader]:
        return (
            self.db.query(FollowedTrader)
            .filter(FollowedTrader.user_id == user_id, FollowedTrader.is_active)
            .order_by(FollowedTrader.created_at.desc())
            .all()
        )

    def save_new(self, record: FollowedTrader) -> FollowedTrader:
        self.add(record, flush=False)
        self.commit()
        return self.refresh(record)

    def save_changes(self, record: FollowedTrader) -> FollowedTrader:
        self.commit()
        return self.refresh(record)
