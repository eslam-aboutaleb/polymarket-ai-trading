"""Forecaster follow-edge queries."""

from sqlalchemy.orm import Session

from app.models.intelligence.collaboration import UserFollow
from app.models.users.user import User


class ForecasterFollowRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def user_exists(self, user_id: int) -> bool:
        return self.db.query(User.id).filter(User.id == user_id).first() is not None

    def get_follow(self, follower_user_id: int, followee_user_id: int) -> UserFollow | None:
        return (
            self.db.query(UserFollow)
            .filter(
                UserFollow.follower_user_id == follower_user_id,
                UserFollow.followee_user_id == followee_user_id,
            )
            .first()
        )

    def delete_follow(self, follower_user_id: int, followee_user_id: int) -> int:
        return int(
            self.db.query(UserFollow)
            .filter(
                UserFollow.follower_user_id == follower_user_id,
                UserFollow.followee_user_id == followee_user_id,
            )
            .delete()
        )

    def list_follows(self, follower_user_id: int) -> list[UserFollow]:
        return (
            self.db.query(UserFollow)
            .filter(UserFollow.follower_user_id == follower_user_id)
            .order_by(UserFollow.created_at.desc())
            .all()
        )

    def followee_ids(self, follower_user_id: int) -> list[int]:
        rows = (
            self.db.query(UserFollow.followee_user_id)
            .filter(UserFollow.follower_user_id == follower_user_id)
            .all()
        )
        return [row.followee_user_id for row in rows]

    def users_by_ids(self, user_ids: list[int]) -> list[User]:
        return self.db.query(User).filter(User.id.in_(user_ids)).all()
