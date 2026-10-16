"""Compatibility exports for the canonical user settings repository."""

from app.repositories.users.settings import SettingsRepository, UserSettingsRepository

__all__ = ["SettingsRepository", "UserSettingsRepository"]
