"""Trading correctness: token_id, idempotent order ingest.

- ``user_trades.token_id``: the CLOB condition token a trade
  was placed on, indexed for fill-reconciliation lookups.
- ``order_idempotency_keys``: durable (user_id, key) -> result
  fallback for order idempotency when Redis is unavailable.
- Unique partial index on ``trade_history(source_trade_id_ext)``
  so the same source trade can never be ingested twice.  The
  index is built CONCURRENTLY (Postgres) outside the migration
  transaction, after duplicate rows are removed.

Revision ID: 20261003_0003
Revises: 20261003_0002
Create Date: 2026-10-03 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0003"
down_revision = "20261003_0002"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # user_trades.token_id (F4).  Every statement is
    # guarded: earlier migrations in this chain run
    # Base.metadata.create_all(checkfirst=True) against
    # the current models, so a fresh database already
    # has this column and its index by the time this
    # migration runs.
    user_trade_columns = {column["name"] for column in inspector.get_columns("user_trades")}
    if "token_id" not in user_trade_columns:
        op.add_column(
            "user_trades",
            sa.Column("token_id", sa.String(length=200), nullable=True),
        )
    user_trade_indexes = {index["name"] for index in inspector.get_indexes("user_trades")}
    if "ix_user_trades_token_id" not in user_trade_indexes:
        op.create_index(
            "ix_user_trades_token_id",
            "user_trades",
            ["token_id"],
            unique=False,
        )

    # order_idempotency_keys (F2 DB fallback)
    if "order_idempotency_keys" not in inspector.get_table_names():
        op.create_table(
            "order_idempotency_keys",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=200), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "idempotency_key",
                name="uq_order_idempotency_user_key",
            ),
        )
        op.create_index(
            "ix_order_idempotency_keys_user_id",
            "order_idempotency_keys",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_order_idempotency_keys_idempotency_key",
            "order_idempotency_keys",
            ["idempotency_key"],
            unique=False,
        )

    # Idempotent trade ingest (F4): dedupe on source_trade_id_ext,
    # keeping the highest id (the newest ingest), then build the
    # unique partial index.  CONCURRENTLY (Postgres) runs outside
    # the migration transaction so trade_history is not locked
    # for writes while the index builds.
    op.execute(
        """
        DELETE FROM trade_history
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_trade_id_ext
                           ORDER BY id DESC
                       ) AS rn
                FROM trade_history
                WHERE source_trade_id_ext IS NOT NULL
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )
    trade_history_indexes = {index["name"] for index in inspector.get_indexes("trade_history")}
    if "uq_trade_history_source_trade_id_ext" not in trade_history_indexes:
        if _is_postgres():
            with op.get_context().autocommit_block():
                op.execute(
                    "CREATE UNIQUE INDEX CONCURRENTLY "
                    "uq_trade_history_source_trade_id_ext "
                    "ON trade_history (source_trade_id_ext) "
                    "WHERE source_trade_id_ext IS NOT NULL"
                )
        else:
            # SQLite: plain unique index (NULLs are distinct, so
            # the partial semantics hold without a WHERE clause).
            op.create_index(
                "uq_trade_history_source_trade_id_ext",
                "trade_history",
                ["source_trade_id_ext"],
                unique=True,
            )


def downgrade() -> None:
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_trade_history_source_trade_id_ext")
    else:
        op.drop_index(
            "uq_trade_history_source_trade_id_ext",
            table_name="trade_history",
        )
    op.drop_index(
        "ix_order_idempotency_keys_idempotency_key",
        table_name="order_idempotency_keys",
    )
    op.drop_index(
        "ix_order_idempotency_keys_user_id",
        table_name="order_idempotency_keys",
    )
    op.drop_table("order_idempotency_keys")
    op.drop_index("ix_user_trades_token_id", table_name="user_trades")
    op.drop_column("user_trades", "token_id")
