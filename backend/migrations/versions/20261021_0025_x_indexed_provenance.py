"""Add the indexed-web provenance of the inverse-bot social summary.

The "X" summary the inverse bot stores comes from a search-engine index, not
from an authenticated X feed. These columns record where the summary came
from (source type, coverage, status) and when the index was last checked, so
the API and the UI can label the source truthfully instead of implying that
an authenticated feed was read.

``last_x_status`` distinguishes "ok", "no_matches" (the index held nothing)
and "provider_unavailable" (the provider never answered) — an outage must
not be presented as an absence of social activity.

Revision ID: 20261021_0025_x_indexed_provenance
Revises: 20261020_0022_trading_controls
Create Date: 2026-10-21 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261021_0025_x_indexed_provenance"
down_revision = "20261020_0022_trading_controls"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("last_x_source_type", sa.String(30)),
    ("last_x_coverage", sa.String(40)),
    ("last_x_status", sa.String(30)),
    ("last_x_checked_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("inverse_bot_positions")}
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column("inverse_bot_positions", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("inverse_bot_positions")}
    for name, _column_type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("inverse_bot_positions", name)
