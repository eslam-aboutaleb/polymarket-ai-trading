"""Join the independently applied research/redeem migrations without rewriting history.

Each feature branch carries this identical migration bundle so it works alone
and after any other branch. Existing revision IDs/parents stay unchanged.
The four nullable/data-only migrations are safe on the baseline schema;
model-specific application behavior remains owned by its feature PR.
"""

revision = "20261021_0027_merge_review_heads"
down_revision = (
    "20261021_0023_forecast_scoring_integrity",
    "20261021_0023",
    "20261021_0025_x_indexed_provenance",
    "20261021_0026_unverified_history_runs",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
