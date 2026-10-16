"""Persistence boundary for orders submitted through the external API."""

from sqlalchemy.orm import Session

from app.services.trading.trade_recorder import record_user_trade


def persist_external_order(
    db: Session,
    *,
    user_id: int,
    market_id: str,
    token_id: str,
    action: str,
    amount: float,
    price: float,
    status: str,
    order_hash: str | None,
    account_id: int,
):
    """Record the submitted external order and its audit event atomically."""
    trade = record_user_trade(
        db,
        user_id=user_id,
        market_id=market_id,
        token_id=token_id,
        action=action,
        amount=amount,
        price=price,
        status=status,
        strategy_source="external",
        order_hash=order_hash,
        expected_price=price,
        expected_size=amount,
        account_id=account_id,
    )
    db.commit()
    return trade


def persist_risk_gate_state(db: Session) -> None:
    """Commit safety-state changes made by a rejected order gate."""
    db.commit()


__all__ = ["persist_external_order", "persist_risk_gate_state"]
