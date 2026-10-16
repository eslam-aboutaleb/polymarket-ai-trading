"""Application operations for user profiles and preferences."""

from sqlalchemy.orm import Session

from app.models.users.user import User
from app.models.users.user_settings import AIBackendType, UserSettings
from app.repositories.users.settings import UserSettingsRepository
from app.repositories.users.user_repository import UserRepository


def get_settings(db: Session, user_id: int) -> UserSettings | None:
    return UserSettingsRepository(db).get_for_user(user_id)


def update_ai_settings(
    db: Session,
    user_id: int,
    *,
    ai_backend: str | None,
    kelly_fraction: float | None,
) -> UserSettings:
    return UserSettingsRepository(db).upsert_ai_backend(
        user_id, ai_backend=ai_backend, kelly_fraction=kelly_fraction
    )


def update_copy_trading_settings(db: Session, user_id: int, changes: dict) -> UserSettings:
    repository = UserSettingsRepository(db)
    settings = repository.get_or_create_for_user(
        user_id,
        ai_backend=AIBackendType.LLM_CHAIN.value,
    )
    return repository.update_copy_trading_settings(settings, changes=changes)


def get_profile(db: Session, user_id: int) -> User | None:
    return UserRepository(db).get_by_id(user_id)


def update_profile(
    db: Session,
    user_id: int,
    *,
    display_name: str | None,
    email: str | None,
    phone: str | None,
    profile_picture_url: str | None,
) -> User | None:
    repository = UserRepository(db)
    user = repository.get_by_id(user_id)
    if user is None:
        return None
    return repository.update_profile(
        user,
        display_name=display_name,
        email=email,
        phone=phone,
        profile_picture_url=profile_picture_url,
    )


def update_llm_preferences(
    db: Session, user_id: int, *, provider: str | None, model: str | None
) -> UserSettings:
    return UserSettingsRepository(db).upsert_llm_preferences(
        user_id, provider=provider, model=model
    )


def admin_user_llm_rows(
    db: Session, *, offset: int = 0, limit: int = 50
) -> tuple[int, list[tuple[User, UserSettings | None]]]:
    repository = UserRepository(db)
    return repository.count_users(), repository.list_users_with_llm_settings(
        offset=offset, limit=limit
    )
