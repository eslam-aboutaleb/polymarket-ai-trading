"""Account-scoped trade reads for external clients and exposure summaries."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.trading.user_trade import UserTrade


class ExternalTradeRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_for_owner(
        self,
        user_id: int,
        statuses: tuple[str, ...],
        *,
        account_id: int | None,
        limit: int,
    ) -> list[UserTrade]:
        stmt = select(UserTrade).where(
            UserTrade.user_id == user_id,
            UserTrade.status.in_(statuses),
        )
        if account_id is not None:
            stmt = stmt.where(UserTrade.account_id == account_id)
        stmt = stmt.order_by(UserTrade.id.desc()).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def list_open_exposure(
        self,
        user_id: int,
        statuses: tuple[str, ...],
        *,
        account_id: int | None,
        include_legacy_rows: bool,
        limit: int,
    ) -> list[UserTrade]:
        stmt = select(UserTrade).where(
            UserTrade.user_id == user_id,
            UserTrade.status.in_(statuses),
        )
        if account_id is not None:
            account_filter = UserTrade.account_id == account_id
            if include_legacy_rows:
                account_filter = or_(account_filter, UserTrade.account_id.is_(None))
            stmt = stmt.where(account_filter)
        stmt = stmt.order_by(UserTrade.id.desc()).limit(limit)
        return list(self.db.execute(stmt).scalars().all())
