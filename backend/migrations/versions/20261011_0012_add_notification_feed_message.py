"""Add the free-text message body to notification_feed_events.

Plan 03 price-alert verdicts and plan 02 daily digests carry their
chat/email body in the alert payload's ``message`` field, but the
in-app feed had no column for it, so the NotificationCenter rendered
those events with a label and nothing else. The column is nullable so
existing rows — and every event type that has no body text — keep
working untouched.

Chains after the daily-digest flag migration (20261011_0011) to keep
a single linear alembic head:
..._0011 → _0012 (this).

The DDL is guarded with an sa.inspect check: earlier migrations in the
chain run Base.metadata.create_all(checkfirst=True) against the
CURRENT models, so a fresh database may already contain this column
and an unguarded op.add_column would fail with "duplicate column
name".

Revision ID: 20261011_0012
Revises: 20261011_0011
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0012"
down_revision = "20261011_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("notification_feed_events")}
    if "message" not in existing:
        op.add_column("notification_feed_events", sa.Column("message", sa.Text(), nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("notification_feed_events")}
    if "message" in existing:
        op.drop_column("notification_feed_events", "message")
