"""Convert datetime columns to timezone-aware semantics (Postgres).

Revision ID: 20260228_0003
Revises: 20260228_0002
Create Date: 2026-02-28 22:30:00
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text

revision = "20260228_0003"
down_revision = "20260228_0002"
branch_labels = None
depends_on = None


_TIMESTAMP_COLUMNS: dict[str, list[str]] = {
    "admin_audit_logs": ["performed_at"],
    "assessments": ["created_at"],
    "followed_traders": ["created_at", "updated_at"],
    "inverse_bot_actions": ["executed_at", "created_at"],
    "inverse_bot_positions": [
        "last_evaluated_at",
        "last_reversed_at",
        "created_at",
        "updated_at",
    ],
    "markets": ["end_date", "created_at", "updated_at"],
    "notification_feed_events": ["emailed_at", "created_at"],
    "notification_followed_traders": ["created_at", "updated_at"],
    "refresh_tokens": ["expires_at", "created_at"],
    "stop_loss_orders": ["triggered_at", "created_at", "updated_at"],
    "trade_history": ["timestamp", "created_at"],
    "trader_position_state": ["updated_at"],
    "users": ["created_at", "last_login"],
    "user_settings": ["created_at", "updated_at"],
    "user_trades": ["executed_at", "created_at", "updated_at"],
    "winners": ["last_trade_time", "last_updated"],
}


def _is_tz_aware(col_type) -> bool:
    tz = getattr(col_type, "timezone", None)
    return bool(tz)


def upgrade() -> None:
    bind = op.get_bind()

    # SQLite and other dialects used in local tests don't support safe
    # TIMESTAMPTZ conversion DDL; runtime now writes aware UTC datetimes anyway.
    if bind.dialect.name != "postgresql":
        return

    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    for table_name, column_names in _TIMESTAMP_COLUMNS.items():
        if table_name not in tables:
            continue
        cols = {column["name"]: column["type"] for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            col_type = cols.get(column_name)
            if col_type is None or _is_tz_aware(col_type):
                continue
            op.execute(
                text(
                    f'ALTER TABLE "{table_name}" '
                    f'ALTER COLUMN "{column_name}" '
                    "TYPE TIMESTAMP WITH TIME ZONE "
                    f"USING \"{column_name}\" AT TIME ZONE 'UTC'"
                )
            )


def downgrade() -> None:
    # Intentionally omitted to avoid lossy timezone downgrades.
    pass
