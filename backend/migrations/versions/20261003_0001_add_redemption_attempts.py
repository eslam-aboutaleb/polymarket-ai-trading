"""Add redemption_attempts table for on-chain auto-redeem.

Revision ID: 20261003_0001
Revises: 20260301_0002
Create Date: 2026-10-03 00:00:00
"""

from alembic import op

revision = "20261003_0001"
down_revision = "20260301_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.models.base import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    op.drop_table("redemption_attempts")
