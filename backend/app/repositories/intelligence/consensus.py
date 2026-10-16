"""Forecast-vote, market and annotation queries."""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.intelligence.collaboration import (
    VISIBILITY_PUBLIC,
    ForecastVote,
    MarketAnnotation,
)
from app.models.market_data.market import Market
from app.models.users.user import User


class ConsensusRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_market(self, condition_id: str) -> Market | None:
        return self.db.query(Market).filter(Market.id == condition_id).first()

    def count_recent_votes(self, user_id: int, since: datetime) -> int:
        return int(
            self.db.query(func.count(ForecastVote.id))
            .filter(ForecastVote.user_id == user_id, ForecastVote.created_at >= since)
            .scalar()
            or 0
        )

    def count_votes_since(self, user_id: int, since: datetime) -> int:
        return self.count_recent_votes(user_id, since)

    def latest_vote_sequence(self, user_id: int, condition_id: str) -> int:
        return int(
            self.db.query(func.max(ForecastVote.vote_seq))
            .filter(ForecastVote.user_id == user_id, ForecastVote.condition_id == condition_id)
            .scalar()
            or 0
        )

    def votes_for_market(self, condition_id: str, visibility: str) -> list[ForecastVote]:
        return (
            self.db.query(ForecastVote)
            .filter(
                ForecastVote.condition_id == condition_id, ForecastVote.visibility == visibility
            )
            .order_by(ForecastVote.vote_seq.asc())
            .all()
        )

    def votes_for_users(
        self, user_ids: list[int], *, visibility: str | None = None, since: datetime | None = None
    ) -> list[ForecastVote]:
        query = self.db.query(ForecastVote).filter(ForecastVote.user_id.in_(user_ids))
        if since is not None:
            query = query.filter(ForecastVote.created_at >= since)
        if visibility is not None:
            query = query.filter(ForecastVote.visibility == visibility)
        return query.order_by(ForecastVote.vote_seq.asc()).all()

    def users_by_ids(self, user_ids: list[int]) -> list[User]:
        return self.db.query(User).filter(User.id.in_(user_ids)).all()

    def market_price(self, condition_id: str) -> float | None:
        row = self.db.query(Market.current_price).filter(Market.id == condition_id).first()
        return row.current_price if row is not None else None

    def active_votes_since(self, cutoff: datetime) -> list[ForecastVote]:
        return (
            self.db.query(ForecastVote)
            .filter(ForecastVote.created_at >= cutoff)
            .order_by(ForecastVote.vote_seq.asc())
            .all()
        )

    def public_votes_for_users_since(
        self, user_ids: list[int], since: datetime
    ) -> list[ForecastVote]:
        return (
            self.db.query(ForecastVote)
            .filter(
                ForecastVote.user_id.in_(user_ids),
                ForecastVote.visibility == VISIBILITY_PUBLIC,
                ForecastVote.created_at >= since,
            )
            .order_by(ForecastVote.vote_seq.desc(), ForecastVote.created_at.desc())
            .all()
        )

    def get_annotation(self, user_id: int, condition_id: str) -> MarketAnnotation | None:
        return (
            self.db.query(MarketAnnotation)
            .filter(
                MarketAnnotation.user_id == user_id, MarketAnnotation.condition_id == condition_id
            )
            .first()
        )

    def public_annotations(self, condition_id: str, visibility: str, limit: int):
        return (
            self.db.query(MarketAnnotation)
            .filter(
                MarketAnnotation.condition_id == condition_id,
                MarketAnnotation.visibility == visibility,
            )
            .order_by(MarketAnnotation.updated_at.desc())
            .limit(limit)
            .all()
        )
