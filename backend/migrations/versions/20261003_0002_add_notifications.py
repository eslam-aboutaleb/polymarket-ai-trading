"""Add notification channels, deliveries, and preferences.

Also adds ``read_at`` to notification_feed_events for in-app
unread state and widens ``event_type`` to fit the full alert
catalog (e.g. ``followed_trader_activity``).

Revision ID: 20261003_0002
Revises: 20261003_0001
Create Date: 2026-10-03 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0002"
down_revision = "20261003_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.models.base import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    # Widen event_type for the full alert catalog. batch_alter_table
    # keeps this SQLite-compatible (table recreate) and emits a plain
    # ALTER COLUMN on Postgres.
    with op.batch_alter_table("notification_feed_events") as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.String(length=20),
            type_=sa.String(length=40),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_feed_events") as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.String(length=40),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
        batch_op.drop_column("read_at")
    op.drop_table("user_notification_preferences")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_channels")
