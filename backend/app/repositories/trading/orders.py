"""Repository queries for trade execution, risk and order monitoring."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from app.models.analytics.assessment import Assessment
from app.models.market_data.market import Market
from app.models.market_data.winner import Winner
from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.stop_loss import StopLossOrder
from app.models.trading.take_profit import TakeProfitOrder
from app.models.trading.trade_history import TradeHistory
from app.models.trading.trader_position_state import TraderPositionState
from app.models.trading.user_trade import UserTrade
from app.repositories.base import SQLAlchemyRepository
from app.utils.time import utc_now


class TradingOrderRepository(SQLAlchemyRepository[object]):
    """Intent-named reads for execution, copy-trading and monitors."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def sum_copy_trade_pnl(
        self,
        user_id: int,
        *,
        since: datetime | None = None,
        negative_only: bool = False,
        account_id: int | None = None,
    ) -> float:
        filters = [
            UserTrade.user_id == user_id,
            UserTrade.copied_from_wallet.isnot(None),
        ]
        if since is not None:
            filters.append(UserTrade.executed_at >= since)
        if negative_only:
            filters.append(UserTrade.pnl < 0)
        if account_id is not None:
            filters.append(UserTrade.account_id == account_id)
        return float(
            self.db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0)).filter(*filters).scalar()
            or 0.0
        )

    def sum_pending_user_trade_amount(self, user_id: int, account_id: int | None = None) -> float:
        filters = [UserTrade.user_id == user_id, UserTrade.status == "pending"]
        if account_id is not None:
            filters.append(UserTrade.account_id == account_id)
        return float(
            self.db.query(func.coalesce(func.sum(UserTrade.amount), 0.0)).filter(*filters).scalar()
            or 0.0
        )

    def sum_strategy_daily_loss(self, user_id: int, strategy_source: str, since: datetime) -> float:
        return float(
            self.db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.strategy_source == strategy_source,
                UserTrade.executed_at >= since,
                UserTrade.pnl < 0,
            )
            .scalar()
            or 0.0
        )

    def list_recent_strategy_trades(
        self, user_id: int, strategy_source: str, limit: int
    ) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.strategy_source == strategy_source,
                UserTrade.pnl.isnot(None),
            )
            .order_by(UserTrade.created_at.desc())
            .limit(limit)
            .all()
        )

    def list_strategy_trades(
        self, strategy_source: str, status: str, pnl_is_null: bool
    ) -> list[UserTrade]:
        pnl_filter = UserTrade.pnl.is_(None) if pnl_is_null else UserTrade.pnl.isnot(None)
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.strategy_source == strategy_source,
                UserTrade.status == status,
                pnl_filter,
            )
            .all()
        )

    def list_trade_rows_for_position_lifecycle(
        self, user_id: int
    ) -> list[tuple[str, str, datetime | None]]:
        return (
            self.db.query(UserTrade.market_id, UserTrade.action, UserTrade.executed_at)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.action.in_(("buy", "sell")),
            )
            .order_by(UserTrade.executed_at.asc())
            .all()
        )

    def list_duplicate_buy_groups(self, user_id: int) -> list[Any]:
        return (
            self.db.query(
                UserTrade.market_id,
                func.count(UserTrade.id).label("count"),
                func.sum(UserTrade.amount * UserTrade.price).label("total_cost"),
                func.sum(UserTrade.amount).label("total_amount"),
            )
            .filter(UserTrade.user_id == user_id, UserTrade.action == "buy")
            .group_by(UserTrade.market_id)
            .having(func.count(UserTrade.id) > 1)
            .all()
        )

    def latest_market_assessment(self, market_id: str) -> Assessment | None:
        return (
            self.db.query(Assessment)
            .join(TradeHistory, TradeHistory.id == Assessment.trade_history_id)
            .filter(TradeHistory.market_id == market_id)
            .order_by(Assessment.created_at.desc())
            .first()
        )

    def find_winner(self, wallet_address: str) -> Winner | None:
        return self.db.query(Winner).filter(Winner.wallet_address == wallet_address).first()

    def list_executed_trade_pnls(self, user_id: int) -> list[tuple[float | None]]:
        return (
            self.db.query(UserTrade.pnl)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.pnl.isnot(None),
                UserTrade.status == "executed",
            )
            .all()
        )

    def find_user_follow(self, user_id: int, wallet: str) -> FollowedTrader | None:
        return (
            self.db.query(FollowedTrader)
            .filter(
                FollowedTrader.user_id == user_id,
                FollowedTrader.trader_wallet == wallet,
                FollowedTrader.is_active,
            )
            .first()
        )

    def list_active_followed_wallets(self) -> list[tuple[str]]:
        return (
            self.db.query(FollowedTrader.trader_wallet)
            .filter(FollowedTrader.is_active)
            .distinct()
            .all()
        )

    def list_active_wallets(self) -> list[str]:
        """Return distinct wallets watched by any active copy-trade follow."""
        wallets = []
        for row in self.list_active_followed_wallets():
            w = row[0] if isinstance(row, (tuple, list)) else getattr(row, "trader_wallet", None)
            if w:
                wallets.append(w)
        return wallets

    def list_active_follows_for_wallet(self, wallet: str) -> list[FollowedTrader]:
        return (
            self.db.query(FollowedTrader)
            .filter(FollowedTrader.trader_wallet == wallet, FollowedTrader.is_active)
            .all()
        )

    def list_active_follows_for_user(self, user_id: int) -> list[FollowedTrader]:
        return (
            self.db.query(FollowedTrader)
            .filter(FollowedTrader.user_id == user_id, FollowedTrader.is_active)
            .order_by(FollowedTrader.created_at.desc())
            .all()
        )

    def list_active_followed_wallets_for_user(self, user_id: int) -> list[str]:
        rows = (
            self.db.query(FollowedTrader.trader_wallet)
            .filter(FollowedTrader.user_id == user_id, FollowedTrader.is_active)
            .all()
        )
        return [row.trader_wallet for row in rows]

    def list_copied_trades(self, user_id: int, limit: int) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(UserTrade.user_id == user_id, UserTrade.copied_from_wallet.isnot(None))
            .order_by(UserTrade.created_at.desc())
            .limit(limit)
            .all()
        )

    def list_source_trades(self, wallet: str, limit: int) -> list[TradeHistory]:
        return (
            self.db.query(TradeHistory)
            .filter(TradeHistory.wallet_address == wallet)
            .order_by(TradeHistory.timestamp.desc())
            .limit(limit)
            .all()
        )

    def list_trade_history_by_wallet(self, wallet: str, limit: int) -> list[TradeHistory]:
        return (
            self.db.query(TradeHistory)
            .filter(TradeHistory.wallet_address == wallet)
            .order_by(TradeHistory.timestamp.desc())
            .limit(limit)
            .all()
        )

    def list_copies_for_source_ids(
        self, user_id: int, wallet: str, source_ids: list[int]
    ) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.copied_from_wallet == wallet,
                UserTrade.source_trade_history_id.in_(source_ids),
            )
            .order_by(UserTrade.created_at.desc())
            .all()
        )

    def sum_daily_copy_pnl(self, user_id: int, since: datetime) -> float:
        return float(
            self.db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.copied_from_wallet.isnot(None),
                UserTrade.executed_at >= since,
            )
            .scalar()
            or 0.0
        )

    def list_pending_order_hash(self, order_hash: str) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(UserTrade.order_hash == order_hash, UserTrade.status == "pending")
            .all()
        )

    def list_old_pending_trades(self, cutoff: datetime) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.status == "pending",
                UserTrade.created_at < cutoff,
                UserTrade.order_hash.isnot(None),
            )
            .all()
        )

    def list_market_trades_for_position(self, user_id: int, market_id: str) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(UserTrade.user_id == user_id, UserTrade.market_id == market_id)
            .all()
        )

    def get_market(self, market_id: str) -> Market | None:
        return self.db.query(Market).filter(Market.id == market_id).first()

    def get_or_create_trader_position_state(
        self, trader_wallet: str, token_id: str
    ) -> TraderPositionState | None:
        return (
            self.db.query(TraderPositionState)
            .filter(
                TraderPositionState.trader_wallet == trader_wallet,
                TraderPositionState.token_id == token_id,
            )
            .first()
        )

    def list_active_stop_losses_with_user(self) -> list[StopLossOrder]:
        return (
            self.db.query(StopLossOrder)
            .options(joinedload(StopLossOrder.user))
            .filter(StopLossOrder.status == "active")
            .all()
        )

    def list_active_take_profits_with_user(self) -> list[TakeProfitOrder]:
        return (
            self.db.query(TakeProfitOrder)
            .options(joinedload(TakeProfitOrder.user))
            .filter(TakeProfitOrder.status == "active")
            .all()
        )

    def get_active_take_profit_by_token(
        self, user_id: int, token_id: str
    ) -> TakeProfitOrder | None:
        return (
            self.db.query(TakeProfitOrder)
            .filter(
                TakeProfitOrder.user_id == user_id,
                TakeProfitOrder.token_id == token_id,
                TakeProfitOrder.status == "active",
            )
            .first()
        )

    def find_active_stop_loss(self, user_id: int, token_id: str) -> StopLossOrder | None:
        return (
            self.db.query(StopLossOrder)
            .filter(
                StopLossOrder.user_id == user_id,
                StopLossOrder.token_id == token_id,
                StopLossOrder.status == "active",
            )
            .first()
        )

    def save_stop_loss(self, order: StopLossOrder, *, refresh: bool = True) -> StopLossOrder:
        self.add(order, flush=False)
        self.commit()
        return self.refresh(order) if refresh else order

    def list_stop_losses(self, user_id: int, status: str | None = None) -> list[StopLossOrder]:
        query = self.db.query(StopLossOrder).filter(StopLossOrder.user_id == user_id)
        if status is not None:
            query = query.filter(StopLossOrder.status == status)
        return query.order_by(StopLossOrder.created_at.desc()).all()

    def find_stop_loss(self, order_id: int, user_id: int) -> StopLossOrder | None:
        return (
            self.db.query(StopLossOrder)
            .filter(StopLossOrder.id == order_id, StopLossOrder.user_id == user_id)
            .first()
        )

    def find_active_take_profit(self, user_id: int, token_id: str) -> TakeProfitOrder | None:
        return (
            self.db.query(TakeProfitOrder)
            .filter(
                TakeProfitOrder.user_id == user_id,
                TakeProfitOrder.token_id == token_id,
                TakeProfitOrder.status == "active",
            )
            .first()
        )

    def save_take_profit(self, order: TakeProfitOrder, *, refresh: bool = True) -> TakeProfitOrder:
        self.add(order, flush=False)
        self.commit()
        return self.refresh(order) if refresh else order

    def list_take_profits(self, user_id: int, status: str | None = None) -> list[TakeProfitOrder]:
        query = self.db.query(TakeProfitOrder).filter(TakeProfitOrder.user_id == user_id)
        if status is not None:
            query = query.filter(TakeProfitOrder.status == status)
        return query.order_by(TakeProfitOrder.created_at.desc()).all()

    def find_take_profit(self, order_id: int, user_id: int) -> TakeProfitOrder | None:
        return (
            self.db.query(TakeProfitOrder)
            .filter(TakeProfitOrder.id == order_id, TakeProfitOrder.user_id == user_id)
            .first()
        )

    def cancel_active_price_orders(self, user_id: int, updated_at: datetime) -> tuple[int, int]:
        stop_losses = (
            self.db.query(StopLossOrder)
            .filter(StopLossOrder.user_id == user_id, StopLossOrder.status == "active")
            .update({"status": "cancelled", "updated_at": updated_at})
        )
        take_profits = (
            self.db.query(TakeProfitOrder)
            .filter(TakeProfitOrder.user_id == user_id, TakeProfitOrder.status == "active")
            .update({"status": "cancelled", "updated_at": updated_at})
        )
        return stop_losses, take_profits

    def cancel_active_stop_losses(self, user_id: int, updated_at: datetime) -> int:
        return (
            self.db.query(StopLossOrder)
            .filter(StopLossOrder.user_id == user_id, StopLossOrder.status == "active")
            .update({"status": "cancelled", "updated_at": updated_at})
        )

    def cancel_active_take_profits(self, user_id: int, updated_at: datetime) -> int:
        return (
            self.db.query(TakeProfitOrder)
            .filter(TakeProfitOrder.user_id == user_id, TakeProfitOrder.status == "active")
            .update({"status": "cancelled", "updated_at": updated_at})
        )

    def count_latency_arb_trades(self, user_id: int, strategy_source: str) -> int:
        query = self.db.query(UserTrade).filter(
            UserTrade.user_id == user_id,
            UserTrade.strategy_source == strategy_source,
        )
        return query.count()

    def list_latency_arb_trades(
        self, user_id: int, strategy_source: str, offset: int, limit: int
    ) -> list[UserTrade]:
        return (
            self.db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.strategy_source == strategy_source,
            )
            .order_by(UserTrade.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def get_by_idempotency_key(self, model: type, user_id: int | None, key: str) -> Any | None:
        """Lookup the idempotency ORM row (model remains service-defined for compatibility)."""
        return (
            self.db.query(model)
            .filter(model.user_id == user_id, model.idempotency_key == key)
            .first()
        )

    def save_new_trade(self, trade: UserTrade) -> UserTrade:
        self.add(trade, flush=False)
        self.commit()
        return self.refresh(trade)

    def copied_trader_aliases(self, user_id: int, wallets: list[str]) -> list[Any]:
        lowered = sorted({wallet.lower() for wallet in wallets if wallet})
        if not lowered:
            return []
        return (
            self.db.query(
                func.lower(FollowedTrader.trader_wallet).label("wallet"),
                FollowedTrader.trader_alias,
            )
            .filter(
                FollowedTrader.user_id == user_id,
                func.lower(FollowedTrader.trader_wallet).in_(lowered),
                FollowedTrader.trader_alias.isnot(None),
            )
            .all()
        )

    def copied_trader_stats(self, user_id: int, cutoff: datetime) -> list[Any]:
        wallet_key = func.lower(UserTrade.copied_from_wallet)
        closed_win = case((UserTrade.pnl > 0, 1), else_=0)
        closed_loss = case((UserTrade.pnl < 0, 1), else_=0)
        still_open = case(
            ((UserTrade.pnl.is_(None)) & (UserTrade.action == "buy"), 1),
            else_=0,
        )
        realized_pnl = func.coalesce(func.sum(UserTrade.pnl), 0.0)
        return (
            self.db.query(
                wallet_key.label("wallet"),
                func.count(UserTrade.id).label("trade_count"),
                realized_pnl.label("realized_pnl"),
                func.coalesce(func.sum(closed_win), 0).label("wins"),
                func.coalesce(func.sum(closed_loss), 0).label("losses"),
                func.coalesce(func.sum(UserTrade.amount), 0.0).label("volume_usdc"),
                func.coalesce(func.sum(still_open), 0).label("open_positions"),
                func.max(UserTrade.executed_at).label("last_trade_at"),
            )
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.copied_from_wallet.isnot(None),
                UserTrade.executed_at >= cutoff,
            )
            .group_by(wallet_key)
            .order_by(realized_pnl.desc(), wallet_key.asc())
            .all()
        )

    def count_unreconciled_copy_sells(
        self, user_id: int, cutoff: datetime, statuses: tuple[str, ...]
    ) -> int:
        return (
            self.db.query(func.count(UserTrade.id))
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.copied_from_wallet.isnot(None),
                UserTrade.executed_at >= cutoff,
                UserTrade.action == "sell",
                UserTrade.status.in_(statuses),
                UserTrade.pnl.is_(None),
            )
            .scalar()
            or 0
        )

    def count_copy_trades_without_fill_time(self, user_id: int) -> int:
        return (
            self.db.query(func.count(UserTrade.id))
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.copied_from_wallet.isnot(None),
                UserTrade.executed_at.is_(None),
            )
            .scalar()
            or 0
        )


class _PersistMixin:
    def save_new(self, record):
        self.add(record, flush=False)
        self.commit()
        return self.refresh(record)

    def save_changes(self, record):
        self.commit()
        return self.refresh(record)


class StopLossRepository(_PersistMixin, TradingOrderRepository):
    def find_active_for_token(self, user_id: int, token_id: str) -> StopLossOrder | None:
        return self.find_active_stop_loss(user_id, token_id)

    def list_for_user(self, user_id: int, *, status: str | None = None) -> list[StopLossOrder]:
        return self.list_stop_losses(user_id, status)

    def find_by_id_for_user(self, order_id: int, user_id: int) -> StopLossOrder | None:
        return self.find_stop_loss(order_id, user_id)

    def cancel_all_active(self, user_id: int) -> int:
        return self.cancel_active_stop_losses(user_id, utc_now())


class TakeProfitRepository(_PersistMixin, TradingOrderRepository):
    def find_active_for_token(self, user_id: int, token_id: str) -> TakeProfitOrder | None:
        return self.find_active_take_profit(user_id, token_id)

    def list_for_user(self, user_id: int, *, status: str | None = None) -> list[TakeProfitOrder]:
        return self.list_take_profits(user_id, status)

    def find_by_id_for_user(self, order_id: int, user_id: int) -> TakeProfitOrder | None:
        return self.find_take_profit(order_id, user_id)

    def cancel_all_active(self, user_id: int) -> int:
        return self.cancel_active_take_profits(user_id, utc_now())


class UserTradeRepository(_PersistMixin, TradingOrderRepository):
    """Compatibility repository for persisting user-trade records."""

    def save_new(self, trade: UserTrade) -> UserTrade:
        return self.save_new_trade(trade)
