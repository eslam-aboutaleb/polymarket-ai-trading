"""Add the venue_markets cross-venue cache (plan 05 Phase A).

``VenueMarket`` is one row per ``(venue, market_id)`` holding the latest
normalized snapshot the venue refresh loop wrote. It is the history the
cross-venue comparison reads, so Phase B's mismatch detection and the
UI's adapter-health panel both query it instead of calling venues live.

The table is additive — nothing is backfilled and no existing table is
touched — and idempotent via ``sa.inspect`` so a re-run against a
database that already has it is a no-op. The unique constraint ships
inside the table definition rather than an ``ALTER TABLE`` (the pattern
that broke migration 20261011_0014 on SQLite).

Chains after the accounts migration (20261011_0015) to keep a single
linear alembic head:
..._0015 → 20261011_0016 (this).

Revision ID: 20261011_0016
Revises: 20261011_0015
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0016"
down_revision = "20261011_0015"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "venue_markets" in _existing_tables():
        return

    op.create_table(
        "venue_markets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("venue", sa.String(length=40), nullable=False),
        sa.Column("market_id", sa.String(length=200), nullable=False),
        sa.Column("question", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("prices_json", sa.Text(), nullable=True),
        sa.Column("volume_24h", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("liquidity", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # One live snapshot per venue market — the refresh writer's
        # idempotency guarantee.
        sa.UniqueConstraint("venue", "market_id", name="uq_venue_markets_venue_market"),
    )
    op.create_index("ix_venue_markets_id", "venue_markets", ["id"])
    op.create_index("ix_venue_markets_venue_fetched", "venue_markets", ["venue", "fetched_at"])


def downgrade() -> None:
    if "venue_markets" not in _existing_tables():
        return
    op.drop_index("ix_venue_markets_venue_fetched", table_name="venue_markets")
    op.drop_index("ix_venue_markets_id", table_name="venue_markets")
    op.drop_table("venue_markets")
