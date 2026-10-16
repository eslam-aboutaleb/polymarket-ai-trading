"""Application reads for user-configured analysis preferences."""

from sqlalchemy.orm import Session

from app.models.users.user_settings import UserSettings
from app.repositories.users.settings import UserPreferencesRepository


def get_user_settings(db: Session, user_id: int) -> UserSettings | None:
    """Return the user's preference record through its persistence port."""
    return UserPreferencesRepository(db).get_settings(user_id)
