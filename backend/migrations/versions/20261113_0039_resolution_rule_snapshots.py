"""Point-in-time resolution rule snapshot table."""

from alembic import op

from app.models.analytics.resolution_rule_snapshot import ResolutionRuleSnapshot

revision = "20261113_0039_resolution_rules"
down_revision = "20261027_0038_paper_intents"
branch_labels = None
depends_on = None


def upgrade():
    ResolutionRuleSnapshot.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    ResolutionRuleSnapshot.__table__.drop(op.get_bind(), checkfirst=True)
