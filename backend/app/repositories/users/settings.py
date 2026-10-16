"""User and account settings persistence queries."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models.users.account_settings import AccountSettings
from app.models.users.user_settings import AIBackendType, LLMProviderType, UserSettings
from app.repositories.base import SQLAlchemyRepository
from app.utils.time import utc_now


class UserSettingsRepository(SQLAlchemyRepository[UserSettings]):
    """Reads and writes for user preferences and account settings."""

    model = UserSettings

    def get_for_user(self, user_id: int) -> UserSettings | None:
        return self.db.query(UserSettings).filter(UserSettings.user_id == user_id).first()

    def get_settings(self, user_id: int) -> UserSettings | None:
        """Compatibility name used by intelligence and market-data services."""
        return self.get_for_user(user_id)

    def get_for_account(self, account_id: int) -> AccountSettings | None:
        stmt = select(AccountSettings).where(AccountSettings.account_id == account_id).limit(1)
        return self.db.execute(stmt).scalars().first()

    def get_or_create_for_user(self, user_id: int, **defaults: Any) -> UserSettings:
        """Load or stage settings without committing the caller's transaction."""
        settings = self.get_for_user(user_id)
        if settings is None:
            settings = UserSettings(user_id=user_id, **defaults)
            self.add(settings)
        return settings

    def upsert_ai_backend(
        self,
        user_id: int,
        *,
        ai_backend: str | None,
        kelly_fraction: float | None,
    ) -> UserSettings:
        settings = self.get_for_user(user_id)
        if settings is None:
            settings = UserSettings(
                user_id=user_id,
                ai_backend=ai_backend or AIBackendType.LLM_CHAIN.value,
                kelly_fraction=kelly_fraction,
            )
            self.add(settings)
        else:
            if ai_backend:
                settings.ai_backend = ai_backend
            if kelly_fraction is not None:
                settings.kelly_fraction = kelly_fraction
            settings.updated_at = utc_now()
        self.commit()
        return self.refresh(settings)

    def update_copy_trading_settings(
        self, settings: UserSettings, *, changes: dict[str, Any]
    ) -> UserSettings:
        for field, value in changes.items():
            setattr(settings, field, value)

        initial_capital = changes.get("initial_capital")
        if initial_capital is not None and (
            settings.peak_capital is None or settings.peak_capital < initial_capital
        ):
            settings.peak_capital = initial_capital

        settings.updated_at = utc_now()
        self.commit()
        return self.refresh(settings)

    def upsert_llm_preferences(
        self,
        user_id: int,
        *,
        provider: str | None,
        model: str | None,
    ) -> UserSettings:
        settings = self.get_for_user(user_id)
        if settings is None:
            settings = UserSettings(
                user_id=user_id,
                ai_backend=AIBackendType.LLM_CHAIN.value,
                preferred_llm_provider=provider.lower() if provider else None,
                preferred_llm_model=model if model else None,
            )
            self.add(settings)
        else:
            if provider is not None:
                settings.preferred_llm_provider = provider.lower() if provider else None
            if model is not None:
                settings.preferred_llm_model = model if model else None
            if provider:
                settings.ai_backend = (
                    AIBackendType.CLI_AGENT.value
                    if provider.lower() == LLMProviderType.GITHUB_MODELS.value
                    else AIBackendType.LLM_CHAIN.value
                )
            settings.updated_at = utc_now()
        self.commit()
        return self.refresh(settings)


# Compatibility names retained while callers move to the canonical repository.
SettingsRepository = UserSettingsRepository
UserPreferencesRepository = UserSettingsRepository

__all__ = [
    "SettingsRepository",
    "UserPreferencesRepository",
    "UserSettingsRepository",
]
