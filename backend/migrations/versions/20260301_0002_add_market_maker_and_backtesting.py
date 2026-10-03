"""Add market_maker_configs and backtest_runs tables.

Revision ID: 20260301_0002
Revises: 20260301_0001
Create Date: 2026-03-01 12:00:00
"""

from alembic import op

revision = "20260301_0002"
down_revision = "20260301_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.models.base import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    op.drop_table("backtest_runs")
    op.drop_table("market_maker_configs")
