"""Selective copy decision that always ends at the risk gateway (issue #6).

A trade is copied only if the trader's shrunk skill is positive on a scored
sample, enough *independent* votes agree, and the follower would not chase the
price beyond ``max_chase``. The approved size is then passed through
``apply_global_safety_caps``; the gateway can only reduce or reject it. This
module never places or routes an order. It is paper/shadow decision
support.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.services.trading.pre_trade_gate import apply_global_safety_caps


def selective_copy_decision(
    *,
    profile: dict[str, Any],
    independent_votes: int,
    side: str,
    leader_price: Decimal,
    current_price: Decimal,
    requested_size: float,
    settings: Any,
    db: Session,
    account_id: int | None = None,
    min_votes: int = 1,
    max_chase: Decimal = Decimal("0.02"),
) -> dict[str, Any]:
    reasons: list[str] = []
    if profile.get("status") != "scored":
        reasons.append("trader_not_scored_watch_only")
    elif profile.get("skill", 0) <= 0:
        reasons.append("no_positive_shrunk_skill")
    if independent_votes < min_votes:
        reasons.append("insufficient_independent_votes")
    chase = current_price - leader_price if side == "BUY" else leader_price - current_price
    if chase > max_chase:
        reasons.append("price_chase_exceeds_max")
    if reasons:
        return {"copy": False, "size": 0.0, "reasons": reasons, "gateway": "not_consulted"}
    size, rejection = apply_global_safety_caps(settings, requested_size, db, account_id)
    if rejection or size <= 0:
        return {
            "copy": False,
            "size": 0.0,
            "reasons": ["risk_gateway_rejected"],
            "gateway": rejection or "zero_size",
        }
    return {"copy": True, "size": size, "reasons": [], "gateway": "approved"}
