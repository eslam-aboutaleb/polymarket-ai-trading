"""Collaborative forecasting: annotations, votes, user follows (plan 02).

Three additive tables that turn one-way copy trading into collective
forecasting:

* ``market_annotations`` — one editable research note per user per
  market, private unless published,
* ``forecast_votes`` — an append-only log of probability estimates whose
  highest ``vote_seq`` per ``(user, condition)`` is the user's active
  forecast,
* ``user_follows`` — user→user follows, a different edge from
  ``followed_traders`` (which copies external wallets by address).

Every table carries ``user_id`` **and** a nullable ``account_id`` per
master plan 5.1: ownership is not a foreign key yet, so no NOT NULL
account column ships in the same migration, and the two foreign keys that
do exist (to ``users``) are declared without a cascade — deleting a user
is not a supported operation, and a follow or a vote is the audit record
of a decision.

The uniqueness constraints ship inside ``create_table`` rather than as
``ALTER TABLE … ADD CONSTRAINT`` — the spelling that made 20261011_0014
break on SQLite. Every constraint named here matches the model's
``__table_args__`` one-for-one, so ``alembic compare_metadata`` stays
drift-free.

Guarded by ``sa.inspect`` so a re-run against a database that already has
these tables is a no-op.

Chains after the api-keys migration (20261011_0017) to keep a single
linear alembic head:
..._0017 → 20261011_0019 (this).

Revision ID: 20261011_0019
Revises: 20261011_0017
Create Date: 2026-10-12 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0019"
down_revision = "20261011_0018"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _existing_tables()

    if "market_annotations" not in tables:
        op.create_table(
            "market_annotations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            # Additive + nullable (master 5.1): no FK, so backfilling and
            # later account work never rewrite this table.
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("condition_id", sa.String(length=100), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("probability", sa.Float(), nullable=True),
            sa.Column(
                "visibility",
                sa.String(length=20),
                nullable=False,
                server_default="private",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            # One editable note per user per market: a second PUT updates
            # the same row, it does not create a second opinion.
            sa.UniqueConstraint(
                "user_id", "condition_id", name="uq_market_annotations_user_market"
            ),
            sa.CheckConstraint(
                "visibility IN ('private', 'public')",
                name="ck_market_annotations_visibility",
            ),
            sa.CheckConstraint(
                "(probability IS NULL OR (probability > 0 AND probability < 1))",
                name="ck_market_annotations_probability_range",
            ),
        )
        op.create_index("ix_market_annotations_id", "market_annotations", ["id"])
        op.create_index("ix_market_annotations_user_id", "market_annotations", ["user_id"])
        op.create_index("ix_market_annotations_account_id", "market_annotations", ["account_id"])
        op.create_index(
            "ix_market_annotations_condition_id", "market_annotations", ["condition_id"]
        )
        op.create_index("ix_market_annotations_created_at", "market_annotations", ["created_at"])
        op.create_index(
            "ix_market_annotations_user_created",
            "market_annotations",
            ["user_id", "created_at"],
        )
        op.create_index(
            "ix_market_annotations_condition_visibility",
            "market_annotations",
            ["condition_id", "visibility"],
        )

    if "forecast_votes" not in tables:
        op.create_table(
            "forecast_votes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            # Additive + nullable (master 5.1): no FK.
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("condition_id", sa.String(length=100), nullable=False),
            sa.Column("probability", sa.Float(), nullable=False),
            sa.Column("vote_seq", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "visibility",
                sa.String(length=20),
                nullable=False,
                server_default="private",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            # The append-only vote log's concurrency guard: a lost race
            # for "next revision" becomes a unique violation instead of
            # two rows that both look like the user's latest forecast.
            sa.UniqueConstraint(
                "user_id",
                "condition_id",
                "vote_seq",
                name="uq_forecast_votes_user_market_seq",
            ),
            sa.CheckConstraint(
                "visibility IN ('private', 'public')",
                name="ck_forecast_votes_visibility",
            ),
            sa.CheckConstraint(
                "(probability > 0 AND probability < 1)",
                name="ck_forecast_votes_probability_range",
            ),
            sa.CheckConstraint("vote_seq >= 1", name="ck_forecast_votes_seq_positive"),
        )
        op.create_index("ix_forecast_votes_id", "forecast_votes", ["id"])
        op.create_index("ix_forecast_votes_user_id", "forecast_votes", ["user_id"])
        op.create_index("ix_forecast_votes_account_id", "forecast_votes", ["account_id"])
        op.create_index("ix_forecast_votes_condition_id", "forecast_votes", ["condition_id"])
        op.create_index("ix_forecast_votes_created_at", "forecast_votes", ["created_at"])
        # The consensus read: one market, public rows, highest revision.
        op.create_index(
            "ix_forecast_votes_condition_visibility",
            "forecast_votes",
            ["condition_id", "visibility", "vote_seq"],
        )
        op.create_index(
            "ix_forecast_votes_user_created", "forecast_votes", ["user_id", "created_at"]
        )

    if "user_follows" not in tables:
        op.create_table(
            "user_follows",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("follower_user_id", sa.Integer(), nullable=False),
            # Additive + nullable (master 5.1): no FK.
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("followee_user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["follower_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["followee_user_id"], ["users.id"]),
            sa.UniqueConstraint(
                "follower_user_id",
                "followee_user_id",
                name="uq_user_follows_pair",
            ),
            # A self-follow is refused in the schema, not only in the UI.
            sa.CheckConstraint(
                "follower_user_id <> followee_user_id",
                name="ck_user_follows_not_self",
            ),
        )
        op.create_index("ix_user_follows_id", "user_follows", ["id"])
        op.create_index("ix_user_follows_follower_user_id", "user_follows", ["follower_user_id"])
        op.create_index("ix_user_follows_account_id", "user_follows", ["account_id"])
        op.create_index("ix_user_follows_followee_user_id", "user_follows", ["followee_user_id"])
        op.create_index(
            "ix_user_follows_followee_created",
            "user_follows",
            ["followee_user_id", "created_at"],
        )


def downgrade() -> None:
    tables = _existing_tables()

    if "user_follows" in tables:
        op.drop_index("ix_user_follows_followee_created", table_name="user_follows")
        op.drop_index("ix_user_follows_followee_user_id", table_name="user_follows")
        op.drop_index("ix_user_follows_account_id", table_name="user_follows")
        op.drop_index("ix_user_follows_follower_user_id", table_name="user_follows")
        op.drop_index("ix_user_follows_id", table_name="user_follows")
        op.drop_table("user_follows")

    if "forecast_votes" in tables:
        op.drop_index("ix_forecast_votes_user_created", table_name="forecast_votes")
        op.drop_index("ix_forecast_votes_condition_visibility", table_name="forecast_votes")
        op.drop_index("ix_forecast_votes_created_at", table_name="forecast_votes")
        op.drop_index("ix_forecast_votes_condition_id", table_name="forecast_votes")
        op.drop_index("ix_forecast_votes_account_id", table_name="forecast_votes")
        op.drop_index("ix_forecast_votes_user_id", table_name="forecast_votes")
        op.drop_index("ix_forecast_votes_id", table_name="forecast_votes")
        op.drop_table("forecast_votes")

    if "market_annotations" in tables:
        op.drop_index("ix_market_annotations_condition_visibility", table_name="market_annotations")
        op.drop_index("ix_market_annotations_user_created", table_name="market_annotations")
        op.drop_index("ix_market_annotations_created_at", table_name="market_annotations")
        op.drop_index("ix_market_annotations_condition_id", table_name="market_annotations")
        op.drop_index("ix_market_annotations_account_id", table_name="market_annotations")
        op.drop_index("ix_market_annotations_user_id", table_name="market_annotations")
        op.drop_index("ix_market_annotations_id", table_name="market_annotations")
        op.drop_table("market_annotations")
