"""Application services used by the trading HTTP routes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.notifications.notification_followed_trader import NotificationFollowedTrader
from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.user_trade import UserTrade
from app.repositories.notifications import NotificationFeedRepository, NotificationFollowRepository
from app.repositories.trading.follows import TradingFollowRepository
from app.repositories.trading.orders import TradingOrderRepository
from app.utils.time import utc_now


def followed_wallets_for_user(db: Session, user_id: int) -> tuple[list[str], list[str]]:
    return (
        TradingFollowRepository(db).list_active_wallets_for_user(user_id),
        NotificationFollowRepository(db).list_active_wallets_for_user(user_id),
    )


def _any_active_watcher(
    follows: TradingFollowRepository,
    notification_follows: NotificationFollowRepository,
    wallet: str,
) -> bool:
    normalized = wallet.lower()
    return follows.has_active_watcher(normalized) or notification_follows.has_active_watcher(
        normalized
    )


def follow_trader(
    db: Session,
    user_id: int,
    wallet: str,
    *,
    changes: dict[str, Any],
    provided_fields: set[str],
) -> FollowedTrader:
    repository = TradingFollowRepository(db)
    normalized_wallet = wallet.lower()
    normalized_alias = changes.get("trader_alias")
    if "trader_alias" in provided_fields and normalized_alias is not None:
        normalized_alias = normalized_alias.strip() or None
    record = repository.find_follow(user_id, normalized_wallet)
    if record is None:
        record = FollowedTrader(
            user_id=user_id,
            trader_wallet=normalized_wallet,
            is_active=True,
            max_position_size=changes.get("max_position_size"),
            trader_alias=normalized_alias if "trader_alias" in provided_fields else None,
            sizing_mode=changes.get("sizing_mode") or "inherit_global",
            fixed_trade_amount_override=changes.get("fixed_trade_amount_override"),
            copy_wallet_mode=changes.get("copy_wallet_mode") or "dynamic_main_wallet_percentage",
            copy_wallet_percentage=changes.get("copy_wallet_percentage")
            if changes.get("copy_wallet_percentage") is not None
            else 100.0,
            copy_wallet_fixed_amount=changes.get("copy_wallet_fixed_amount"),
        )
        return repository.save_new(record)

    record.is_active = True
    if "max_position_size" in provided_fields:
        record.max_position_size = changes.get("max_position_size")
    if "trader_alias" in provided_fields:
        record.trader_alias = normalized_alias
    if "sizing_mode" in provided_fields:
        record.sizing_mode = changes.get("sizing_mode") or "inherit_global"
    if "fixed_trade_amount_override" in provided_fields:
        record.fixed_trade_amount_override = changes.get("fixed_trade_amount_override")
    if "copy_wallet_mode" in provided_fields:
        record.copy_wallet_mode = (
            changes.get("copy_wallet_mode") or "dynamic_main_wallet_percentage"
        )
    if "copy_wallet_percentage" in provided_fields:
        record.copy_wallet_percentage = (
            changes.get("copy_wallet_percentage")
            if changes.get("copy_wallet_percentage") is not None
            else 100.0
        )
    if "copy_wallet_fixed_amount" in provided_fields:
        record.copy_wallet_fixed_amount = changes.get("copy_wallet_fixed_amount")
    record.updated_at = utc_now()
    return repository.save_changes(record)


def unfollow_trader(db: Session, user_id: int, wallet: str) -> tuple[bool, bool]:
    repository = TradingFollowRepository(db)
    record = repository.find_follow(user_id, wallet.lower())
    if record is None:
        return False, False
    record.is_active = False
    record.updated_at = utc_now()
    repository.commit()
    return True, not _any_active_watcher(repository, NotificationFollowRepository(db), wallet)


def list_following(db: Session, user_id: int) -> list[FollowedTrader]:
    return TradingFollowRepository(db).list_active_follows(user_id)


def notification_follow_trader(
    db: Session,
    user_id: int,
    wallet: str,
    *,
    feed_enabled: bool | None,
    email_enabled: bool | None,
    provided_fields: set[str],
) -> NotificationFollowedTrader:
    repository = NotificationFollowRepository(db)
    normalized_wallet = wallet.lower()
    record = repository.find_follow(user_id, normalized_wallet)
    if record is not None:
        record.is_active = True
        if "feed_enabled" in provided_fields and feed_enabled is not None:
            record.feed_enabled = bool(feed_enabled)
        if "email_enabled" in provided_fields and email_enabled is not None:
            record.email_enabled = bool(email_enabled)
        if not record.feed_enabled and not record.email_enabled:
            record.feed_enabled = True
        record.updated_at = utc_now()
        return repository.save_changes(record)

    record = NotificationFollowedTrader(
        user_id=user_id,
        trader_wallet=normalized_wallet,
        is_active=True,
        feed_enabled=bool(feed_enabled) if feed_enabled is not None else True,
        email_enabled=bool(email_enabled) if email_enabled is not None else False,
    )
    if not record.feed_enabled and not record.email_enabled:
        record.feed_enabled = True
    return repository.save_new(record)


def unfollow_notifications(db: Session, user_id: int, wallet: str) -> tuple[bool, bool]:
    repository = NotificationFollowRepository(db)
    record = repository.find_follow(user_id, wallet.lower())
    if record is None:
        return False, False
    record.is_active = False
    record.feed_enabled = False
    record.email_enabled = False
    record.updated_at = utc_now()
    repository.commit()
    return True, not _any_active_watcher(TradingFollowRepository(db), repository, wallet)


def list_notification_following(db: Session, user_id: int) -> list[NotificationFollowedTrader]:
    return NotificationFollowRepository(db).list_active_follows(user_id)


def list_following_feed(
    db: Session,
    user_id: int,
    *,
    wallet: str | None,
    event_type: str | None,
    limit: int,
):
    return NotificationFeedRepository(db).list_recent(
        user_id,
        wallet=wallet,
        event_type=event_type,
        limit=limit,
    )


def record_manual_trade(
    db: Session,
    *,
    user_id: int,
    market_id: str,
    token_id: str,
    action: str,
    amount: float,
    price: float,
    status: str,
    order_hash: str | None,
) -> UserTrade:
    """Persist an order submission result through the trading repository."""
    trade = UserTrade(
        user_id=user_id,
        market_id=market_id,
        token_id=token_id,
        action=action,
        amount=amount,
        price=price,
        status=status,
        order_hash=order_hash,
        expected_price=price,
        expected_size=amount,
        strategy_source="manual",
        executed_at=None,
    )
    repository = TradingOrderRepository(db)
    try:
        return repository.save_new_trade(trade)
    except Exception:
        repository.rollback()
        raise
