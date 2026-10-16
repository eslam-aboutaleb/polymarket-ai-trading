"""Repository reads for notification-only followed traders."""

from __future__ import annotations

from app.models.notifications.notification_followed_trader import NotificationFollowedTrader
from app.repositories.base import SQLAlchemyRepository


class NotificationFollowRepository(SQLAlchemyRepository[NotificationFollowedTrader]):
    model = NotificationFollowedTrader

    def list_active_wallets(self, user_id: int | None = None) -> list[str]:
        if user_id is not None:
            return self.list_active_wallets_for_user(user_id)
        rows = (
            self.db.query(NotificationFollowedTrader.trader_wallet)
            .filter(NotificationFollowedTrader.is_active)
            .distinct()
            .all()
        )
        wallets = []
        for row in rows:
            w = row[0] if isinstance(row, (tuple, list)) else getattr(row, "trader_wallet", None)
            if w:
                wallets.append(w)
        return wallets

    def list_active_wallets_for_user(self, user_id: int) -> list[str]:
        rows = (
            self.db.query(NotificationFollowedTrader.trader_wallet)
            .filter(
                NotificationFollowedTrader.user_id == user_id,
                NotificationFollowedTrader.is_active,
            )
            .all()
        )
        return [row.trader_wallet for row in rows]

    def notification_watcher_exists(self, wallet: str) -> bool:
        return self.has_active_watcher(wallet)

    def list_active_for_wallet(self, wallet: str) -> list[NotificationFollowedTrader]:
        return (
            self.db.query(NotificationFollowedTrader)
            .filter(
                NotificationFollowedTrader.trader_wallet == wallet,
                NotificationFollowedTrader.is_active,
            )
            .all()
        )

    def has_active_watcher(self, wallet: str) -> bool:
        return (
            self.db.query(NotificationFollowedTrader)
            .filter(
                NotificationFollowedTrader.trader_wallet == wallet,
                NotificationFollowedTrader.is_active,
            )
            .first()
            is not None
        )

    def find_follow(self, user_id: int, wallet: str) -> NotificationFollowedTrader | None:
        return (
            self.db.query(NotificationFollowedTrader)
            .filter(
                NotificationFollowedTrader.user_id == user_id,
                NotificationFollowedTrader.trader_wallet == wallet,
            )
            .first()
        )

    def list_active_follows(self, user_id: int) -> list[NotificationFollowedTrader]:
        return (
            self.db.query(NotificationFollowedTrader)
            .filter(
                NotificationFollowedTrader.user_id == user_id,
                NotificationFollowedTrader.is_active,
            )
            .order_by(NotificationFollowedTrader.created_at.desc())
            .all()
        )

    def save_new(self, record: NotificationFollowedTrader) -> NotificationFollowedTrader:
        self.add(record, flush=False)
        self.commit()
        return self.refresh(record)

    def save_changes(self, record: NotificationFollowedTrader) -> NotificationFollowedTrader:
        self.commit()
        return self.refresh(record)
