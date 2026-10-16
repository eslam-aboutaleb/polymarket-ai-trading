"""Use cases for stop-loss, take-profit and trading safety controls."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.trading.stop_loss import StopLossOrder
from app.models.trading.take_profit import TakeProfitOrder
from app.models.users.user_settings import UserSettings
from app.repositories.trading.bots import TradingBotRepository
from app.repositories.trading.orders import TradingOrderRepository


def set_stop_loss(
    db: Session,
    *,
    user_id: int,
    token_id: str,
    market_id: str,
    market_title: str,
    outcome: str,
    size: float,
    stop_price: float,
    now: datetime,
) -> StopLossOrder:
    repository = TradingOrderRepository(db)
    row = repository.find_active_stop_loss(user_id, token_id)
    if row is None:
        row = StopLossOrder(
            user_id=user_id,
            token_id=token_id,
            market_id=market_id,
            market_title=market_title,
            outcome=outcome,
            size=size,
            stop_price=stop_price,
            status="active",
        )
    else:
        row.stop_price = stop_price
        row.size = size
        row.market_title = market_title or row.market_title
        row.outcome = outcome or row.outcome
        row.updated_at = now
    return repository.save_stop_loss(row)


def list_stop_losses(db: Session, user_id: int, status: str | None) -> list[StopLossOrder]:
    return TradingOrderRepository(db).list_stop_losses(user_id, status)


def cancel_stop_loss(
    db: Session,
    *,
    order_id: int,
    user_id: int,
    now: datetime,
) -> tuple[str, StopLossOrder | None]:
    repository = TradingOrderRepository(db)
    row = repository.find_stop_loss(order_id, user_id)
    if row is None:
        return "missing", None
    if row.status != "active":
        return "inactive", row
    row.status = "cancelled"
    row.updated_at = now
    repository.save_stop_loss(row, refresh=False)
    return "cancelled", row


def set_take_profit(
    db: Session,
    *,
    user_id: int,
    token_id: str,
    market_id: str,
    market_title: str,
    outcome: str,
    size: float,
    take_profit_price: float,
    now: datetime,
) -> TakeProfitOrder:
    repository = TradingOrderRepository(db)
    row = repository.find_active_take_profit(user_id, token_id)
    if row is None:
        row = TakeProfitOrder(
            user_id=user_id,
            token_id=token_id,
            market_id=market_id,
            market_title=market_title,
            outcome=outcome,
            size=size,
            take_profit_price=take_profit_price,
            status="active",
        )
    else:
        row.take_profit_price = take_profit_price
        row.size = size
        row.market_title = market_title or row.market_title
        row.outcome = outcome or row.outcome
        row.updated_at = now
    return repository.save_take_profit(row)


def list_take_profits(db: Session, user_id: int, status: str | None) -> list[TakeProfitOrder]:
    return TradingOrderRepository(db).list_take_profits(user_id, status)


def cancel_take_profit(
    db: Session,
    *,
    order_id: int,
    user_id: int,
    now: datetime,
) -> tuple[str, TakeProfitOrder | None]:
    repository = TradingOrderRepository(db)
    row = repository.find_take_profit(order_id, user_id)
    if row is None:
        return "missing", None
    if row.status != "active":
        return "inactive", row
    row.status = "cancelled"
    row.updated_at = now
    repository.save_take_profit(row, refresh=False)
    return "cancelled", row


def halt_user_trading(
    db: Session,
    user_id: int,
    *,
    now: datetime,
) -> tuple[UserSettings, int, int, bool]:
    """Halt copy trading and cancel active conditional orders atomically."""
    bot_repository = TradingBotRepository(db)
    settings = bot_repository.get_user_settings(user_id)
    created = settings is None
    if settings is None:
        settings = UserSettings(
            user_id=user_id,
            copy_trading_enabled=False,
            trading_halted=True,
            halt_reason="Emergency stop triggered by user",
        )
        bot_repository.add(settings, flush=False)
    else:
        settings.copy_trading_enabled = False
        settings.trading_halted = True
        settings.halt_reason = "Emergency stop triggered by user"
        settings.updated_at = now
    stop_losses, take_profits = TradingOrderRepository(db).cancel_active_price_orders(
        user_id,
        now,
    )
    bot_repository.commit()
    return settings, stop_losses, take_profits, created


def resume_user_trading(
    db: Session,
    user_id: int,
    *,
    now: datetime,
) -> UserSettings | None:
    settings = TradingBotRepository(db).get_user_settings(user_id)
    if settings is None:
        return None
    settings.trading_halted = False
    settings.halt_reason = None
    settings.cooldown_until = None
    if not settings.copy_trading_enabled:
        settings.copy_trading_enabled = True
    settings.updated_at = now
    TradingBotRepository(db).commit()
    return settings


def get_or_create_trading_settings(
    db: Session,
    user_id: int,
    *,
    commit_new: bool,
) -> UserSettings:
    settings = TradingBotRepository(db).get_user_settings(user_id)
    if settings is not None:
        return settings
    settings = UserSettings(user_id=user_id, copy_trading_enabled=False)
    return TradingBotRepository(db).add_user_settings(settings, commit=commit_new)


def get_trading_user(db: Session, user_id: int):
    return TradingBotRepository(db).get_user(user_id)


def commit_trading_settings(db: Session) -> None:
    """Persist state changes made by a trading safety decision."""
    TradingBotRepository(db).commit()
