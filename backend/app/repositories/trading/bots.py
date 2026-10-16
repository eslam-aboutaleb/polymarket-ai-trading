"""Repository queries for trading bots and their user context."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.trading.inverse_bot_position import InverseBotPosition
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.market_maker_config import MarketMakerConfig
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.repositories.base import SQLAlchemyRepository


class TradingBotRepository(SQLAlchemyRepository[object]):
    """Named reads used by bot services and route-facing use cases."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_user(self, user_id: int) -> User | None:
        return self.db.query(User).filter(User.id == user_id).first()

    def list_users(self) -> list[User]:
        return self.db.query(User).all()

    def list_users_by_ids(self, user_ids: list[int]) -> list[User]:
        return self.db.query(User).filter(User.id.in_(user_ids)).all()

    def list_user_id_wallets(self) -> list[tuple[int, str]]:
        return self.db.query(User.id, User.wallet_address).all()

    def get_user_settings(self, user_id: int) -> UserSettings | None:
        return self.db.query(UserSettings).filter(UserSettings.user_id == user_id).first()

    def add_user_settings(
        self,
        settings: UserSettings,
        *,
        commit: bool,
    ) -> UserSettings:
        self.add(settings, flush=False)
        if commit:
            self.commit()
        else:
            self.db.flush()
        return settings

    def list_users_with_settings_ids(self) -> list[tuple[int]]:
        return self.db.query(UserSettings.user_id).all()

    def list_inverse_bot_user_ids(self) -> list[int]:
        rows = self.db.query(UserSettings.user_id).filter(UserSettings.inverse_bot_enabled).all()
        return [row.user_id for row in rows]

    def list_settings_for_users(self, user_ids: list[int]) -> list[UserSettings]:
        return self.db.query(UserSettings).filter(UserSettings.user_id.in_(user_ids)).all()

    def get_market_maker_config(self, config_id: int) -> MarketMakerConfig | None:
        return self.db.query(MarketMakerConfig).filter(MarketMakerConfig.id == config_id).first()

    def find_market_maker_config(self, user_id: int, condition_id: str) -> MarketMakerConfig | None:
        return (
            self.db.query(MarketMakerConfig)
            .filter(
                MarketMakerConfig.user_id == user_id,
                MarketMakerConfig.condition_id == condition_id,
            )
            .first()
        )

    def find_market_maker_config_for_user(
        self, config_id: int, user_id: int
    ) -> MarketMakerConfig | None:
        return (
            self.db.query(MarketMakerConfig)
            .filter(
                MarketMakerConfig.id == config_id,
                MarketMakerConfig.user_id == user_id,
            )
            .first()
        )

    def list_market_maker_configs(self, user_id: int) -> list[MarketMakerConfig]:
        return (
            self.db.query(MarketMakerConfig)
            .filter(MarketMakerConfig.user_id == user_id)
            .order_by(MarketMakerConfig.created_at.desc())
            .all()
        )

    def list_enabled_market_maker_configs(self) -> list[MarketMakerConfig]:
        return self.db.query(MarketMakerConfig).filter(MarketMakerConfig.enabled).all()

    def save_market_maker_config(
        self, config: MarketMakerConfig, *, refresh: bool = False
    ) -> MarketMakerConfig:
        self.add(config, flush=False)
        self.commit()
        return self.refresh(config) if refresh else config

    def find_latency_arb_config(self, user_id: int) -> LatencyArbConfig | None:
        return self.db.query(LatencyArbConfig).filter(LatencyArbConfig.user_id == user_id).first()

    def save_latency_arb_config(
        self, config: LatencyArbConfig, *, refresh: bool = False
    ) -> LatencyArbConfig:
        self.add(config, flush=False)
        self.commit()
        return self.refresh(config) if refresh else config

    def list_enabled_latency_arb_configs(self) -> list[LatencyArbConfig]:
        return self.db.query(LatencyArbConfig).filter(LatencyArbConfig.enabled.is_(True)).all()

    def list_enabled_latency_arb_accounts(
        self,
    ) -> list[tuple[LatencyArbConfig, UserSettings, User]]:
        return (
            self.db.query(LatencyArbConfig, UserSettings, User)
            .join(UserSettings, UserSettings.user_id == LatencyArbConfig.user_id)
            .join(User, User.id == LatencyArbConfig.user_id)
            .filter(LatencyArbConfig.enabled.is_(True))
            .all()
        )

    def list_inverse_positions(self, user_id: int) -> list[InverseBotPosition]:
        return (
            self.db.query(InverseBotPosition)
            .filter(InverseBotPosition.user_id == user_id)
            .order_by(InverseBotPosition.created_at.desc())
            .all()
        )

    def find_inverse_position(self, user_id: int, token_id: str) -> InverseBotPosition | None:
        return (
            self.db.query(InverseBotPosition)
            .filter(
                InverseBotPosition.user_id == user_id,
                InverseBotPosition.token_id == token_id,
            )
            .first()
        )

    def find_inverse_position_for_user(
        self, position_id: int, user_id: int
    ) -> InverseBotPosition | None:
        return (
            self.db.query(InverseBotPosition)
            .filter(
                InverseBotPosition.id == position_id,
                InverseBotPosition.user_id == user_id,
            )
            .first()
        )

    def list_enabled_inverse_positions(self, user_id: int) -> list[InverseBotPosition]:
        return (
            self.db.query(InverseBotPosition)
            .filter(
                InverseBotPosition.user_id == user_id,
                InverseBotPosition.enabled,
            )
            .all()
        )

    def save_inverse_position(
        self, position: InverseBotPosition, *, refresh: bool = False
    ) -> InverseBotPosition:
        self.add(position, flush=False)
        self.commit()
        return self.refresh(position) if refresh else position
