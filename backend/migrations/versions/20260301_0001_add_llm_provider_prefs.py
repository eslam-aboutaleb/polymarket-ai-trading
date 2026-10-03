"""Add LLM provider preference columns to user_settings

Revision ID: 20260301_0001
Revises: 20260228_0003
Create Date: 2026-03-01
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260301_0001"
down_revision = "20260228_0003"
branch_labels = None
depends_on = None

_COLUMN_DEFINITIONS = (
    ("preferred_llm_provider", sa.Column("preferred_llm_provider", sa.String(30), nullable=True)),
    ("preferred_llm_model", sa.Column("preferred_llm_model", sa.String(100), nullable=True)),
)


def _existing_columns() -> set:
    """Names of the columns currently present on ``user_settings``."""
    inspector = sa.inspect(op.get_bind())
    if "user_settings" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("user_settings")}


def upgrade() -> None:
    """Add preferred_llm_provider and preferred_llm_model columns.

    The baseline revision creates the schema from the live SQLAlchemy metadata,
    so on a freshly provisioned database these columns already exist and adding
    them again raises "duplicate column name". Adding is therefore guarded.
    """
    existing = _existing_columns()
    for name, column in _COLUMN_DEFINITIONS:
        if name in existing:
            continue
        op.add_column("user_settings", column)


def downgrade() -> None:
    """Remove preferred_llm_provider and preferred_llm_model columns."""
    existing = _existing_columns()
    for name, _column in reversed(_COLUMN_DEFINITIONS):
        if name in existing:
            op.drop_column("user_settings", name)
