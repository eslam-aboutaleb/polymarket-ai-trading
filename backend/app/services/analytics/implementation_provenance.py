"""Source identity for prospective diagnostics; missing metadata never gates trading."""

import hashlib
from functools import cache
from pathlib import Path


@cache
def implementation_fingerprint():
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    try:
        files = sorted(root.rglob("*.py"))
        if not files:
            return None
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()


def configuration_snapshot(db, user_id, account_id, strategy):
    """Freeze relevant non-secret configuration, excluding portfolio counters."""
    import json

    from sqlalchemy import or_, select

    from app.models.users.account import Account
    from app.models.users.account_settings import AccountSettings
    from app.models.users.user_settings import UserSettings

    names = {
        "ai_backend",
        "preferred_llm_provider",
        "preferred_llm_model",
        "copy_trading_enabled",
        "risk_mode",
        "max_position_size",
        "daily_loss_limit",
        "mirror_percentage",
        "fixed_trade_amount",
        "kelly_fraction",
        "require_ai_approval",
        "monthly_loss_limit",
        "max_drawdown_pct",
        "total_loss_halt_pct",
        "dynamic_sizing_enabled",
        "simulation_mode",
        "inverse_bot_enabled",
        "inverse_bot_default_size_mode",
        "inverse_bot_fixed_amount",
        "inverse_bot_confidence_threshold",
        "inverse_bot_cooldown_minutes",
        "inverse_bot_max_reversals_per_day",
    }

    def fields(row):
        return {name: getattr(row, name, None) for name in sorted(names)} if row else None

    wallet = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    account = (
        db.get(Account, account_id)
        if account_id is not None
        else db.scalar(
            select(Account)
            .where(Account.owner_user_id == user_id, Account.kind == "personal")
            .limit(1)
        )
    )
    effective_account_id = account.id if account else account_id
    sub = (
        db.scalar(
            select(AccountSettings)
            .where(AccountSettings.account_id == account_id)
            .execution_options(populate_existing=True)
        )
        if account and account.kind != "personal"
        else None
    )
    if wallet is None:
        return None
    value = {"wallet": fields(wallet), "account": fields(sub), "strategy": strategy}
    models = []
    if strategy == "market_maker":
        from app.models.trading.market_maker_config import MarketMakerConfig

        models.append(MarketMakerConfig)
    elif strategy == "latency_arb":
        from app.models.trading.latency_arb_config import LatencyArbConfig

        models.append(LatencyArbConfig)
    elif strategy == "copy":
        from app.models.trading.followed_trader import FollowedTrader

        models.append(FollowedTrader)
    excluded = {
        "id",
        "user_id",
        "account_id",
        "created_at",
        "updated_at",
        "status",
        "last_sync_at",
        "last_error",
        "total_orders_placed",
        "total_orders_cancelled",
        "total_volume_usdc",
        "total_pnl",
        "trades_count",
        "last_trade_at",
        "consecutive_wins",
        "consecutive_losses",
        "current_open_orders",
        "trader_alias",
        "market_title",
    }
    for model in models:
        query = select(model).where(model.user_id == user_id)
        if hasattr(model, "account_id"):
            scope = model.account_id == effective_account_id
            if account is None or account.kind == "personal":
                scope = or_(scope, model.account_id.is_(None))
            query = query.where(scope)
        rows = db.scalars(
            query.execution_options(populate_existing=True)
            .order_by(*model.__mapper__.primary_key)
            .limit(501)
        ).all()
        if len(rows) > 500:
            return None
        value[model.__tablename__] = [
            {
                column.name: getattr(row, column.name)
                for column in model.__table__.columns
                if column.name not in excluded
            }
            for row in rows
        ]

    def stable(item):
        if isinstance(item, float):
            import math

            if not math.isfinite(item):
                raise ValueError("nonfinite configuration")
            return repr(item)
        if isinstance(item, dict):
            return {key: stable(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [stable(child) for child in item]
        return item

    try:
        return json.loads(json.dumps(stable(value), sort_keys=True, default=str, allow_nan=False))
    except (TypeError, ValueError):
        return None


def configuration_hash(value):
    import json

    if value is None:
        return None
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        ).encode()
    ).hexdigest()


def configuration_fingerprint(db, user_id, account_id, strategy):
    return configuration_hash(configuration_snapshot(db, user_id, account_id, strategy))
