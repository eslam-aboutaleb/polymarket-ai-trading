"""Quarantine old scores while retaining their original outcome and score as evidence."""

import sqlalchemy as sa
from alembic import op

revision = "20261021_0028_quarantine_legacy_scores"
down_revision = "20261021_0027_merge_review_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE assessments SET exclusion_reason = 'legacy_unverified_resolution' "
            "WHERE brier_score IS NOT NULL AND exclusion_reason IS NULL "
            "AND (scoring_version IS NULL OR scoring_version <> 'brier-binary-v2')"
        )
    )


def downgrade() -> None:
    # This uncertainty is factual, not a reversible claim of verified finality.
    # Keep the quarantine marker; original scores/outcomes were never overwritten.
    pass
