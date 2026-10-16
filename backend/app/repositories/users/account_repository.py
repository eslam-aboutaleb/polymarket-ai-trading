"""Account persistence queries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.users.account import Account, AccountKind


class AccountRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, account_id: int) -> Account | None:
        return self.db.get(Account, account_id)

    def get_by_user_and_id(self, user_id: int, account_id: int) -> Account | None:
        stmt = select(Account).where(
            Account.id == account_id,
            Account.owner_user_id == user_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_personal_for_owner(self, user_id: int) -> Account | None:
        stmt = (
            select(Account)
            .where(Account.owner_user_id == user_id, Account.kind == AccountKind.PERSONAL.value)
            .order_by(Account.id.asc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def get_personal_account(self, user_id: int) -> Account | None:
        return self.get_personal_for_owner(user_id)

    def list_for_owner(self, user_id: int) -> list[Account]:
        stmt = (
            select(Account)
            .where(Account.owner_user_id == user_id)
            .order_by(Account.is_default.desc(), Account.created_at.asc(), Account.id.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def list_for_user(self, user_id: int) -> list[Account]:
        return self.list_for_owner_by_id(user_id)

    def list_for_owner_by_id(self, user_id: int) -> list[Account]:
        stmt = select(Account).where(Account.owner_user_id == user_id).order_by(Account.id.asc())
        return list(self.db.execute(stmt).scalars().all())

    def list_other_defaults(self, user_id: int, keep_id: int) -> list[Account]:
        stmt = select(Account).where(
            Account.owner_user_id == user_id,
            Account.is_default.is_(True),
            Account.id != keep_id,
        )
        return list(self.db.execute(stmt).scalars().all())
