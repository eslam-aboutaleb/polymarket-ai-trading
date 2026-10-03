"""Add on-chain whale monitoring tables.

Creates ``whale_events`` (detected whale-sized CTF position
movements) and ``whale_configs`` (per-user thresholds, watchlist
and auto-copy toggle).

Chains after plan 01's trading-correctness migration
(20261003_0003) to keep a single alembic head.

Revision ID: 20261003_0004
Revises: 20261003_0003
Create Date: 2026-10-03 00:00:00
"""

from alembic import op

revision = "20261003_0004"
down_revision = "20261003_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.models.base import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    op.drop_table("whale_configs")
    op.drop_table("whale_events")
