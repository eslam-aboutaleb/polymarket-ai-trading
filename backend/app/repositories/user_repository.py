"""Compatibility exports for the canonical user repositories.

New code should import from ``app.repositories.users``. This module remains
available for older application and test imports.
"""

from app.repositories.users.settings import UserSettingsRepository
from app.repositories.users.user_repository import UserCreationError, UserRepository

__all__ = ["UserCreationError", "UserRepository", "UserSettingsRepository"]
