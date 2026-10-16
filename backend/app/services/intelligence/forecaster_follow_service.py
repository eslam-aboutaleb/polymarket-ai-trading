"""Forecaster graph (plan 02).

Following *users* rather than external wallets. ``FollowedTrader``
copies a wallet address and measures nothing; a ``UserFollow`` points at
an account whose forecasts are scored by ``consensus_service``, so
"follow" here means "show me their estimates and their track record",
never "mirror their orders".

Every write is the caller's own edge: a follow can only be created or
removed by the follower, a user cannot follow themselves (the schema
refuses it), and the following feed is assembled from *public* votes
only — following someone is not a licence to read their private notes.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.intelligence.collaboration import UserFollow
from app.repositories.intelligence.consensus import ConsensusRepository
from app.repositories.intelligence.followers import ForecasterFollowRepository
from app.services.intelligence.consensus_service import DEFAULT_WINDOW_DAYS, forecaster_skill
from app.utils.time import utc_now

logger = logging.getLogger(__name__)


class FollowRejected(ValueError):
    """A follow/unfollow the service refuses, with a machine-readable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def follow_user(
    db: Session,
    follower_user_id: int,
    followee_user_id: int,
    *,
    account_id: int | None = None,
) -> UserFollow:
    """Follow a forecaster, idempotently.

    A repeat follow returns the existing row rather than creating a
    duplicate, so a double-tap in the UI is a no-op.

    Raises:
        FollowRejected: the caller tried to follow themselves
            (``self_follow``) or the target does not exist
            (``unknown_user``).
    """
    if follower_user_id == followee_user_id:
        raise FollowRejected("self_follow", "A user cannot follow themselves")

    repository = ForecasterFollowRepository(db)
    if not repository.user_exists(followee_user_id):
        raise FollowRejected("unknown_user", "No such forecaster")

    existing = repository.get_follow(follower_user_id, followee_user_id)
    if existing is not None:
        return existing

    row = UserFollow(
        follower_user_id=follower_user_id,
        account_id=account_id,
        followee_user_id=followee_user_id,
        created_at=utc_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def unfollow_user(db: Session, follower_user_id: int, followee_user_id: int) -> bool:
    """Stop following a forecaster. ``False`` when the edge did not exist."""
    deleted = ForecasterFollowRepository(db).delete_follow(follower_user_id, followee_user_id)
    db.commit()
    return bool(deleted)


def list_following(db: Session, user_id: int) -> list[dict[str, Any]]:
    """The users the caller follows, with their measurable track record.

    Ordered by skill then resolved count so the feed leads with the
    forecasters whose estimates are worth reading first.
    """
    follow_repository = ForecasterFollowRepository(db)
    follows = follow_repository.list_follows(user_id)
    if not follows:
        return []
    followee_ids = [row.followee_user_id for row in follows]
    users = {user.id: user for user in follow_repository.users_by_ids(followee_ids)}
    rows = []
    for row in follows:
        user = users.get(row.followee_user_id)
        if user is None:
            continue
        skill = forecaster_skill(db, user.id, DEFAULT_WINDOW_DAYS)
        rows.append(
            {
                "user_id": user.id,
                "display_name": user.display_name,
                "wallet_address": user.wallet_address,
                "followed_at": row.created_at.isoformat() if row.created_at else None,
                "skill": skill["skill"],
                "resolved": skill["resolved"],
                "sufficient_samples": skill["sufficient_samples"],
            }
        )
    rows.sort(key=lambda r: (-r["skill"], -r["resolved"], r["user_id"]))
    return rows


def following_forecasts(
    db: Session,
    user_id: int,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The latest *public* forecasts of the users the caller follows.

    Each row carries the forecaster's measured skill next to their
    estimate, so the feed shows who is talking and how often they have
    been right. Private votes are never included: following somebody
    grants visibility of what they chose to publish.
    """
    follow_repository = ForecasterFollowRepository(db)
    followee_ids = follow_repository.followee_ids(user_id)
    if not followee_ids:
        return []

    cutoff = utc_now() - timedelta(days=max(1, int(window_days)))
    rows = ConsensusRepository(db).public_votes_for_users_since(followee_ids, cutoff)
    latest: dict[tuple[int, str], Any] = {}
    for row in rows:
        latest.setdefault((row.user_id, row.condition_id), row)

    users = {user.id: user for user in follow_repository.users_by_ids(followee_ids)}
    skills = {user_id_: forecaster_skill(db, user_id_, window_days) for user_id_ in followee_ids}

    forecasts = []
    for (followee_id, condition_id), vote in latest.items():
        user = users.get(followee_id)
        skill = skills[followee_id]
        forecasts.append(
            {
                "condition_id": condition_id,
                "probability": round(float(vote.probability), 4),
                "vote_seq": vote.vote_seq,
                "created_at": vote.created_at.isoformat() if vote.created_at else None,
                "forecaster": {
                    "user_id": followee_id,
                    "display_name": user.display_name if user else None,
                    "wallet_address": user.wallet_address if user else None,
                    "skill": skill["skill"],
                    "resolved": skill["resolved"],
                },
            }
        )
    forecasts.sort(key=lambda row: row["created_at"] or "", reverse=True)
    return forecasts[: max(1, int(limit))]
