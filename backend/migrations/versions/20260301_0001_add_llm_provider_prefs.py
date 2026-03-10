"""Add LLM provider preference columns to user_settings

Revision ID: 20260301_0001
Revises: 20260228_0003
Create Date: 2026-03-01
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260301_0001'
down_revision = '20260228_0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add preferred_llm_provider and preferred_llm_model columns."""
    # Add preferred_llm_provider column
    op.add_column(
        'user_settings',
        sa.Column(
            'preferred_llm_provider',
            sa.String(30),
            nullable=True,
            default=None,
        )
    )
    
    # Add preferred_llm_model column
    op.add_column(
        'user_settings',
        sa.Column(
            'preferred_llm_model',
            sa.String(100),
            nullable=True,
            default=None,
        )
    )


def downgrade() -> None:
    """Remove preferred_llm_provider and preferred_llm_model columns."""
    op.drop_column('user_settings', 'preferred_llm_model')
    op.drop_column('user_settings', 'preferred_llm_provider')
