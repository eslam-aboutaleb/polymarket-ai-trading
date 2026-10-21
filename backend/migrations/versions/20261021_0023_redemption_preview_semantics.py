"""Disambiguate historical redemption 'pending' rows; preview is never on-chain pending.

Revision ID: 20261021_0023
Revises: 20261020_0022_trading_controls
"""

from alembic import op
from sqlalchemy import text

revision = "20261021_0023"
down_revision = "20261020_0022_trading_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing pending rows were created by the dry-run branch, but we cannot
    # prove their execution mode from the old schema. Do not call them 'preview'
    # or 'submitted' without verifiable provenance.
    op.get_bind().execute(
        text(
            "UPDATE redemption_attempts SET status = 'legacy_unknown' WHERE status = 'pending'"
        )
    )


def downgrade() -> None:
    # Revert the encoding only; this cannot restore evidence that never existed.
    op.get_bind().execute(
        text(
            "UPDATE redemption_attempts SET status = 'pending' WHERE status = 'legacy_unknown'"
        )
    )
