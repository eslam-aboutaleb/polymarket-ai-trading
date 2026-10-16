"""Collaborative forecasting models (plan 02).

Three tables turn one-way copy trading into collective forecasting:

* ``MarketAnnotation`` — one editable research note per user per market,
  carrying an optional stated probability. A vote without a rationale is
  a number nobody can audit, so the note and the number live together.
* ``ForecastVote`` — an append-only log of probability estimates. It is
  *append-only in spirit* (``vote_seq`` increases on every revision) but
  only the highest ``vote_seq`` per ``(user, condition)`` is the user's
  active forecast, so revising a belief never erases the audit trail and
  a consensus read is exactly "the latest vote per user".
* ``UserFollow`` — a user→user follow, which is a different edge from
  ``followed_traders`` (that one copies *external wallets* by address;
  this one follows a pseudonymous account whose accuracy is measurable
  through the rows above).

Invariants these tables encode rather than trust:

1. **Private by default.** ``visibility`` starts at ``private`` and a
   consensus read only ever selects ``public`` rows. A wallet-linked
   vote is linkable to a wallet, so publishing one has to be a choice
   the forecaster makes, not a default they discover later.
2. **Scoped ownership.** Every table carries ``user_id`` plus a nullable
   ``account_id`` (master 5.1) and the standard ``created_at``.
3. **One vote per market per user, enforced twice.** The app rejects a
   second concurrent revision at the same ``vote_seq`` and the unique
   index on ``(user_id, condition_id, vote_seq)`` makes a duplicate
   sequence a database error rather than a silent double count.
4. **Probabilities are never fabricated.** A vote's probability is
   constrained to the open interval ``(0, 1)``; an annotation's optional
   probability follows the same rule. Nobody can store a "certainty".
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from app.models.base import Base
from app.utils.time import utc_now

#: Only this user can read the row (the caller's own view always may).
VISIBILITY_PRIVATE = "private"
#: The row contributes to consensus and to the public forecaster board.
VISIBILITY_PUBLIC = "public"
VISIBILITY_VALUES = (VISIBILITY_PRIVATE, VISIBILITY_PUBLIC)

# Spelled inline in the CheckConstraint below: a SQLite CHECK is a
# literal it evaluates per row, and formatting the tuple into it would
# make the constraint text depend on import order.
_VISIBILITY_CHECK = "visibility IN ('private', 'public')"
_PROBABILITY_CHECK = "(probability IS NULL OR (probability > 0 AND probability < 1))"


class MarketAnnotation(Base):
    """One user's research note (and optional probability) on a market.

    A single editable row per ``(user, condition_id)``: the note is a
    living document, not a thread, and ``updated_at`` is what tells a
    reader how stale the reasoning is. History is deliberately not kept
    in v1 — a changing opinion is recorded by ``ForecastVote``, which is
    append-only, so the auditable trail lives on the number rather than
    on the prose.
    """

    __tablename__ = "market_annotations"

    id = Column(Integer, primary_key=True, index=True)
    # No cascade: a user row is never deleted, and an annotation is the
    # audit record of what someone believed before a market resolved.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    #: Nullable per master 5.1 — a note belongs to a person first.
    account_id = Column(Integer, nullable=True, index=True)
    condition_id = Column(String(100), nullable=False, index=True)
    body = Column(Text, nullable=False)
    #: Optional stated probability in the open interval (0, 1). ``NULL``
    #: means "reasoning only" and is never coerced to 0.5.
    probability = Column(Float, nullable=True)
    visibility = Column(String(20), nullable=False, default=VISIBILITY_PRIVATE)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        # A unique *index* rather than a column-level ``unique=True``: it
        # is the portable spelling across backends (SQLite cannot ADD a
        # UNIQUE constraint), and the migration creates exactly this
        # index so ``alembic compare_metadata`` stays drift-free.
        Index("uq_market_annotations_user_market", "user_id", "condition_id", unique=True),
        # "my notes, newest first" and "public notes on this market".
        Index("ix_market_annotations_user_created", "user_id", "created_at"),
        Index("ix_market_annotations_condition_visibility", "condition_id", "visibility"),
        CheckConstraint(_VISIBILITY_CHECK, name="ck_market_annotations_visibility"),
        CheckConstraint(_PROBABILITY_CHECK, name="ck_market_annotations_probability_range"),
    )

    def __repr__(self):
        return (
            f"<MarketAnnotation(user={self.user_id}, market={self.condition_id}, "
            f"visibility={self.visibility})>"
        )


class ForecastVote(Base):
    """One probability estimate from one user on one market.

    Append-only: revising a forecast writes the next ``vote_seq`` rather
    than overwriting, so a user can never quietly rewrite the history a
    consensus or an accuracy board was computed from. The active vote is
    the row with the highest ``vote_seq`` for the pair, which is what
    every read path resolves with.

    ``visibility`` is carried per revision, not per user: a forecaster
    who publishes a vote and later revises it privately has withdrawn
    that number from the consensus without deleting it.
    """

    __tablename__ = "forecast_votes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    #: Nullable per master 5.1 — a vote belongs to a person first.
    account_id = Column(Integer, nullable=True, index=True)
    condition_id = Column(String(100), nullable=False, index=True)
    #: Stated probability the YES outcome resolves true, in (0, 1).
    probability = Column(Float, nullable=False)
    #: Monotonic per (user, condition); 1 for the first vote on a market.
    vote_seq = Column(Integer, nullable=False, default=1)
    visibility = Column(String(20), nullable=False, default=VISIBILITY_PRIVATE)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)

    __table_args__ = (
        # One active vote per (user, market) is enforced in the service
        # by allocating ``max(vote_seq) + 1``; this index makes a lost
        # race a database error instead of two "latest" votes.
        Index(
            "uq_forecast_votes_user_market_seq",
            "user_id",
            "condition_id",
            "vote_seq",
            unique=True,
        ),
        # The consensus read: one market, public rows, newest first.
        Index(
            "ix_forecast_votes_condition_visibility",
            "condition_id",
            "visibility",
            "vote_seq",
        ),
        Index("ix_forecast_votes_user_created", "user_id", "created_at"),
        CheckConstraint("visibility IN ('private', 'public')", name="ck_forecast_votes_visibility"),
        CheckConstraint(
            "(probability > 0 AND probability < 1)",
            name="ck_forecast_votes_probability_range",
        ),
        CheckConstraint("vote_seq >= 1", name="ck_forecast_votes_seq_positive"),
    )

    def __repr__(self):
        return (
            f"<ForecastVote(user={self.user_id}, market={self.condition_id}, "
            f"p={self.probability}, seq={self.vote_seq})>"
        )


class UserFollow(Base):
    """A user→user follow: subscribe to another forecaster's estimates.

    Deliberately not ``FollowedTrader``: that table copies *external
    wallets* by address and has no measurable accuracy. A ``UserFollow``
    points at an account whose Brier skill is computable from
    ``ForecastVote``, so "follow" here means "show me their forecasts",
    never "mirror their trades".
    """

    __tablename__ = "user_follows"

    id = Column(Integer, primary_key=True, index=True)
    # No cascade either way: a follow is a record of a decision, and the
    # pair is what the unique index protects.
    follower_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    #: Nullable per master 5.1 — the follow is attributed to the person.
    account_id = Column(Integer, nullable=True, index=True)
    followee_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        Index("uq_user_follows_pair", "follower_user_id", "followee_user_id", unique=True),
        Index("ix_user_follows_followee_created", "followee_user_id", "created_at"),
        # A self-follow would let a user inflate their own follower count
        # and pollute their own following feed, so the schema refuses it.
        CheckConstraint(
            "follower_user_id <> followee_user_id",
            name="ck_user_follows_not_self",
        ),
    )

    def __repr__(self):
        return f"<UserFollow(follower={self.follower_user_id}, followee={self.followee_user_id})>"
