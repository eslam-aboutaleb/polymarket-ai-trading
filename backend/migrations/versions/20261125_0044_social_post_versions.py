"""Insert-only social post versions (#57)."""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0044_social_posts"
down_revision = "20261125_0043_analysis_runs"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("social_post_versions"):
        return
    op.create_table(
        "social_post_versions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("post_id", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("canonical_url", sa.String(300), nullable=False),
        sa.Column("url_aliases_json", sa.Text, nullable=False),
        sa.Column("author_id", sa.String(40)),
        sa.Column("author_handle", sa.String(40)),
        sa.Column("provider_verified", sa.Boolean),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("engagement_json", sa.Text),
        sa.Column("engagement_as_of", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duplicate_of", sa.String(60)),
        sa.Column("provenance", sa.String(20), nullable=False),
        sa.Column("injection_flagged", sa.Boolean, nullable=False),
        sa.UniqueConstraint("provider", "post_id", "version", name="uq_social_post_version"),
    )
    op.create_index("ix_social_post_versions_text_hash", "social_post_versions", ["text_hash"])
    op.create_index("ix_social_post_seen", "social_post_versions", ["first_seen_at"])


def downgrade():
    op.drop_table("social_post_versions")
