"""Add the append-only app_events log (plan 10 Phase A).

``AppEvent`` records the actions that move money or change what the
system is allowed to do. It sits alongside the existing tables — this
is incremental event sourcing, so nothing is backfilled: only events
emitted from the deploy onward exist, and readers must tolerate a
table that starts nearly empty.

The migration is additive and idempotent (``sa.inspect`` guard), so it
is safe to re-run against a database where the table already exists.

Chains after the notification-feed migration (20261011_0012) to keep a
single linear alembic head:
..._0012 → 20261011_0013 (this).

Revision ID: 20261011_0013
Revises: 20261011_0012
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0013"
down_revision = "20261011_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    if "app_events" in existing_tables:
        return

    op.create_table(
        "app_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), nullable=True),
        # Additive and nullable: accounts land in plan 06 Phase A and
        # pre-account rows map to the owner's own personal account,
        # which is a NULL rather than a backfilled value here.
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("aggregate_type", sa.String(length=50), nullable=False),
        sa.Column("aggregate_id", sa.String(length=200), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_events_id", "app_events", ["id"])
    op.create_index("ix_app_events_occurred_at", "app_events", ["occurred_at"])
    op.create_index("ix_app_events_user_id", "app_events", ["user_id"])
    # Composite indexes mirror the query patterns in
    # ``app.services.event_log.get_events`` (per-user timeline and
    # per-aggregate history).
    op.create_index("ix_app_events_aggregate", "app_events", ["aggregate_type", "aggregate_id"])
    op.create_index("ix_app_events_user_occurred", "app_events", ["user_id", "occurred_at"])


def downgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    if "app_events" not in existing_tables:
        return
    op.drop_index("ix_app_events_user_occurred", table_name="app_events")
    op.drop_index("ix_app_events_aggregate", table_name="app_events")
    op.drop_index("ix_app_events_user_id", table_name="app_events")
    op.drop_index("ix_app_events_occurred_at", table_name="app_events")
    op.drop_index("ix_app_events_id", table_name="app_events")
    op.drop_table("app_events")
