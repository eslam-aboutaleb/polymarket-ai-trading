"""Security hardening: refresh-token hashing and DB check constraints.

Revision ID: 20260228_0002
Revises: 20260228_0001
Create Date: 2026-02-28 20:45:00
"""

from __future__ import annotations

import hashlib
import hmac
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "20260228_0002"
down_revision = "20260228_0001"
branch_labels = None
depends_on = None


def _refresh_token_hash_secret() -> str:
    secret = (
        os.environ.get("REFRESH_TOKEN_HASH_SECRET", "").strip()
        or os.environ.get("JWT_SECRET_KEY", "").strip()
    )
    if not secret:
        raise RuntimeError(
            "REFRESH_TOKEN_HASH_SECRET or JWT_SECRET_KEY must be set to migrate refresh tokens."
        )
    return secret


def _hash_refresh_token(raw_token: str) -> str:
    return hmac.new(
        _refresh_token_hash_secret().encode("utf-8"),
        raw_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _ensure_check_constraints(
    inspector,
    *,
    table_name: str,
    constraints: dict[str, str],
) -> None:
    if not _table_exists(inspector, table_name):
        return
    existing = {
        constraint.get("name")
        for constraint in inspector.get_check_constraints(table_name)
        if constraint.get("name")
    }
    for name, sqltext in constraints.items():
        if name not in existing:
            op.create_check_constraint(name, table_name, sqltext)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"

    if _table_exists(inspector, "refresh_tokens"):
        columns = _column_names(inspector, "refresh_tokens")
        if "token_hash" not in columns:
            op.add_column(
                "refresh_tokens",
                sa.Column("token_hash", sa.String(length=64), nullable=True),
            )

        if "token" in columns:
            legacy_rows = (
                bind.execute(
                    text(
                        "SELECT id, token FROM refresh_tokens "
                        "WHERE token_hash IS NULL AND token IS NOT NULL"
                    )
                )
                .mappings()
                .all()
            )
            for row in legacy_rows:
                bind.execute(
                    text("UPDATE refresh_tokens SET token_hash = :token_hash WHERE id = :id"),
                    {"id": row["id"], "token_hash": _hash_refresh_token(row["token"])},
                )

        bind.execute(text("DELETE FROM refresh_tokens WHERE token_hash IS NULL"))

        inspector = inspect(bind)
        columns = _column_names(inspector, "refresh_tokens")
        if "token" in columns:
            for index in inspector.get_indexes("refresh_tokens"):
                name = index.get("name")
                col_names = index.get("column_names") or []
                if name and col_names == ["token"]:
                    op.drop_index(name, table_name="refresh_tokens")

            for unique in inspector.get_unique_constraints("refresh_tokens"):
                name = unique.get("name")
                col_names = unique.get("column_names") or []
                if name and col_names == ["token"]:
                    op.drop_constraint(name, "refresh_tokens", type_="unique")

            if is_sqlite:
                with op.batch_alter_table("refresh_tokens") as batch_op:
                    batch_op.alter_column(
                        "token",
                        existing_type=sa.String(),
                        nullable=True,
                    )
            else:
                op.alter_column(
                    "refresh_tokens",
                    "token",
                    existing_type=sa.String(),
                    nullable=True,
                )
            bind.execute(text("UPDATE refresh_tokens SET token = NULL WHERE token IS NOT NULL"))

        inspector = inspect(bind)
        existing_indexes = {
            tuple(index.get("column_names") or [])
            for index in inspector.get_indexes("refresh_tokens")
        }
        if ("token_hash",) not in existing_indexes:
            op.create_index(
                "ix_refresh_tokens_token_hash",
                "refresh_tokens",
                ["token_hash"],
                unique=True,
            )

        if is_sqlite:
            with op.batch_alter_table("refresh_tokens") as batch_op:
                batch_op.alter_column(
                    "token_hash",
                    existing_type=sa.String(length=64),
                    nullable=False,
                )
        else:
            op.alter_column(
                "refresh_tokens",
                "token_hash",
                existing_type=sa.String(length=64),
                nullable=False,
            )

    _ensure_check_constraints(
        inspector=inspect(bind),
        table_name="user_settings",
        constraints={
            "ck_user_settings_mirror_percentage_range": (
                "(mirror_percentage IS NULL OR (mirror_percentage >= 0 AND mirror_percentage <= 100))"
            ),
            "ck_user_settings_inverse_confidence_range": (
                "(inverse_bot_confidence_threshold >= 0 AND inverse_bot_confidence_threshold <= 100)"
            ),
            "ck_user_settings_max_position_size_non_negative": (
                "(max_position_size IS NULL OR max_position_size >= 0)"
            ),
            "ck_user_settings_daily_loss_limit_non_negative": (
                "(daily_loss_limit IS NULL OR daily_loss_limit >= 0)"
            ),
            "ck_user_settings_fixed_trade_amount_non_negative": (
                "(fixed_trade_amount IS NULL OR fixed_trade_amount >= 0)"
            ),
            "ck_user_settings_inverse_fixed_amount_non_negative": (
                "(inverse_bot_fixed_amount >= 0)"
            ),
            "ck_user_settings_inverse_cooldown_non_negative": (
                "(inverse_bot_cooldown_minutes >= 0)"
            ),
            "ck_user_settings_inverse_max_reversals_non_negative": (
                "(inverse_bot_max_reversals_per_day >= 0)"
            ),
        },
    )

    _ensure_check_constraints(
        inspector=inspect(bind),
        table_name="followed_traders",
        constraints={
            "ck_followed_traders_max_position_size_non_negative": (
                "(max_position_size IS NULL OR max_position_size >= 0)"
            ),
            "ck_followed_traders_fixed_override_non_negative": (
                "(fixed_trade_amount_override IS NULL OR fixed_trade_amount_override >= 0)"
            ),
            "ck_followed_traders_copy_wallet_percentage_range": (
                "(copy_wallet_percentage >= 0 AND copy_wallet_percentage <= 100)"
            ),
            "ck_followed_traders_copy_wallet_fixed_amount_non_negative": (
                "(copy_wallet_fixed_amount IS NULL OR copy_wallet_fixed_amount >= 0)"
            ),
        },
    )


def downgrade() -> None:
    # Downgrade intentionally omitted for safety in production environments.
    pass
