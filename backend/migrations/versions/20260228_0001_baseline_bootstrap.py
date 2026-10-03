"""Baseline bootstrap for Alembic-managed schema.

Revision ID: 20260228_0001
Revises:
Create Date: 2026-02-28 20:40:00
"""

from alembic import op

revision = "20260228_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    import app.models  # noqa: F401
    from app.models.base import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Keep downgrade non-destructive for production safety.
    pass
