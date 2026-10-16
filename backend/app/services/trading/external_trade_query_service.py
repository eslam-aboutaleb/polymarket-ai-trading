"""Trading read operations used by the external API and exposure route."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.trading.user_trade import UserTrade
from app.repositories.trading.external_trade_repository import ExternalTradeRepository


def list_account_trades(
    db: Session,
    *,
    user_id: int,
    account_id: int | None,
    statuses: tuple[str, ...],
    limit: int,
) -> list[UserTrade]:
    return ExternalTradeRepository(db).list_for_owner(
        user_id, statuses, account_id=account_id, limit=limit
    )


def list_exposure_trades(
    db: Session,
    *,
    user_id: int,
    account_id: int | None,
    personal_account: bool,
    statuses: tuple[str, ...],
    limit: int,
) -> list[UserTrade]:
    return ExternalTradeRepository(db).list_open_exposure(
        user_id,
        statuses,
        account_id=account_id,
        include_legacy_rows=personal_account,
        limit=limit,
    )
