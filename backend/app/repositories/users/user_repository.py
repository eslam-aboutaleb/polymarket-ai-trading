"""User data access.

Wallet login and profile flows use this repository for user records.
Settings persistence lives in ``users.settings``.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.repositories.base import SQLAlchemyRepository
from app.utils.time import utc_now


class UserCreationError(RuntimeError):
    """A concurrent wallet login won the insert race and the row vanished.

    Raised when a first-time login insert fails on a unique violation and
    the retry lookup still finds no row. Routes translate it into a 500:
    after the rollback the wallet's user row is expected to exist, so its
    absence is unexpected server state rather than a client error.
    """


class UserRepository(SQLAlchemyRepository[User]):
    """Reads and writes for the ``users`` table."""

    model = User

    def get_by_id(self, user_id: int) -> User | None:
        """Return the user with this id, or ``None``."""
        return self.db.query(User).filter(User.id == user_id).first()

    def find_by_id(self, user_id: int) -> User | None:
        """Compatibility name for service lookup call sites."""
        return self.get_by_id(user_id)

    def list_all(self) -> list[User]:
        """Return all users for bounded startup orchestration tasks."""
        return self.db.query(User).all()

    def get_by_wallet(self, wallet_address: str) -> User | None:
        """Return the user owning this wallet address, or ``None``."""
        return self.db.query(User).filter(User.wallet_address == wallet_address).first()

    def get_or_create_for_login(self, wallet_address: str) -> User:
        """Load the wallet's user, creating it on first login.

        Signature and private-key login resolve the user identically, so
        the insert race is handled here: a unique violation means a
        concurrent login for the same wallet committed first and the
        request adopts that row instead of failing.
        """
        user = self.get_by_wallet(wallet_address)
        if user is not None:
            user.last_login = utc_now()
            self.commit()
            return user
        return self._insert_login_user(wallet_address)

    def _insert_login_user(self, wallet_address: str) -> User:
        user = User(wallet_address=wallet_address, last_login=utc_now())
        try:
            self.add(user)
            self.commit()
        except IntegrityError:
            self.rollback()
            existing = self.get_by_wallet(wallet_address)
            if existing is None:
                raise UserCreationError(wallet_address) from None
            self.refresh(existing)
            return existing
        self.refresh(user)
        return user

    def update_profile(
        self,
        user: User,
        *,
        display_name: str | None,
        email: str | None,
        phone: str | None,
        profile_picture_url: str | None,
    ) -> User:
        """Apply the profile edit and commit it.

        Empty strings mean "clear this field" (stored as NULL), so the
        display name can fall back to the wallet address again.
        """
        if display_name is not None:
            user.display_name = display_name or None
        if email is not None:
            user.email = email or None
        if phone is not None:
            user.phone = phone or None
        if profile_picture_url is not None:
            user.profile_picture_url = profile_picture_url or None
        self.commit()
        return self.refresh(user)

    def update_email(self, user: User, email: str) -> User:
        """Store a verified email and commit at the caller's prior boundary."""
        user.email = email
        self.commit()
        return user

    def count_users(self) -> int:
        """Return the total number of users."""
        return self.db.query(func.count(User.id)).scalar()

    def list_users_with_llm_settings(
        self, *, offset: int = 0, limit: int = 50
    ) -> list[tuple[User, UserSettings | None]]:
        """Return a page of users outer-joined to their LLM settings.

        The outer join is what makes users without a settings row
        listable: the admin view then applies its own fallbacks.
        """
        rows = (
            self.db.query(User, UserSettings)
            .outerjoin(UserSettings, User.id == UserSettings.user_id)
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [(user, settings) for user, settings in rows]
