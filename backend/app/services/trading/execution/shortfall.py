"""Implementation shortfall, fill rate and the FOK vs limit-then-take comparison (#157)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.trading.execution_shortfall import ExecutionShortfall
from app.services.trading.execution.limit_then_take import (
    ZERO,
    Book,
    Decision,
    Execution,
    Params,
    net_edge_per_share,
    taker_fee,
)


def _sign(side: str) -> int:
    return 1 if side == "BUY" else -1


def shortfall_metrics(decision: Decision, execution: Execution) -> dict:
    """Signed so positive means worse than the decision price. Unknown stays None."""
    sign = _sign(decision.side)
    filled = execution.filled
    avg = execution.avg_price
    shortfall = None
    if filled and avg is not None:
        shortfall = sign * (avg - decision.decision_price) * filled + execution.fees
    missed = max(ZERO, decision.shares - filled)
    missed_cost = None
    if execution.final_ref_price is not None:
        missed_cost = sign * (execution.final_ref_price - decision.decision_price) * missed
    return {
        "fill_rate": (filled / decision.shares) if decision.shares else ZERO,
        "shortfall_usdc": shortfall,
        "missed_shares": missed,
        "missed_cost_usdc": missed_cost,
    }


def record_shortfall(
    db: Session,
    *,
    user_id: int,
    decision: Decision,
    execution: Execution,
    mode: str,
    account_id: int | None = None,
) -> ExecutionShortfall:
    """Idempotent per (user, strategy, decision): a retry never writes a second row."""
    existing = db.scalar(
        select(ExecutionShortfall).where(
            ExecutionShortfall.user_id == user_id,
            ExecutionShortfall.strategy == decision.strategy,
            ExecutionShortfall.decision_id == decision.decision_id,
        )
    )
    if existing:
        return existing
    m = shortfall_metrics(decision, execution)
    row = ExecutionShortfall(
        user_id=user_id,
        account_id=account_id,
        strategy=decision.strategy,
        mode=mode,
        decision_id=decision.decision_id,
        token_id=decision.token_id,
        side=decision.side,
        outcome=execution.outcome,
        shares_requested=decision.shares,
        shares_filled=execution.filled,
        maker_shares=execution.maker_filled,
        decision_price=decision.decision_price,
        avg_fill_price=execution.avg_price,
        fees_usdc=execution.fees,
        shortfall_usdc=m["shortfall_usdc"],
        missed_shares=m["missed_shares"],
        missed_cost_usdc=m["missed_cost_usdc"],
    )
    db.add(row)
    db.commit()
    return row


def summarize(db: Session, user_id: int) -> list[dict]:
    """Per strategy and mode: orders, fill rate, mean shortfall per filled share, missed cost."""
    rows = db.execute(
        select(
            ExecutionShortfall.strategy,
            ExecutionShortfall.mode,
            func.count(),
            func.sum(ExecutionShortfall.shares_requested),
            func.sum(ExecutionShortfall.shares_filled),
            func.sum(ExecutionShortfall.shortfall_usdc),
            func.sum(ExecutionShortfall.missed_cost_usdc),
            func.sum(ExecutionShortfall.fees_usdc),
        )
        .where(ExecutionShortfall.user_id == user_id)
        .group_by(ExecutionShortfall.strategy, ExecutionShortfall.mode)
        .order_by(ExecutionShortfall.strategy, ExecutionShortfall.mode)
    ).all()
    out = []
    for strategy, mode, n, req, filled, short, missed, fees in rows:
        req, filled = Decimal(str(req or 0)), Decimal(str(filled or 0))
        out.append(
            {
                "strategy": strategy,
                "mode": mode,
                "orders": n,
                "fill_rate": str(filled / req) if req else None,
                "shortfall_usdc": None if short is None else str(Decimal(str(short))),
                "shortfall_per_filled_share": (
                    str(Decimal(str(short)) / filled) if short is not None and filled else None
                ),
                "missed_cost_usdc": None if missed is None else str(Decimal(str(missed))),
                "fees_usdc": str(Decimal(str(fees or 0))),
            }
        )
    return out


# ───────────── paper comparison: same decisions, both modes, after fees ─────────────


@dataclass(frozen=True)
class ComparisonCase:
    decision: Decision
    book_at_decision: Book
    depth_at_touch: Decimal  # shares available at the taker price at decision time
    book_at_timeout: Book
    depth_at_timeout: Decimal
    # Shares of ATTRIBUTED venue flow that traded through our limit after the recorded
    # queue ahead. None or 0 means no evidence, so no maker fill is assumed.
    attributed_maker_shares: Decimal | None = None


def _value(decision: Decision, price: Decimal, shares: Decimal) -> Decimal:
    return _sign(decision.side) * (decision.fair_value - price) * shares


def compare_modes(cases: list[ComparisonCase], params: Params) -> dict:
    """Replay the same decisions through FOK and limit-then-take. Maker fills need evidence."""
    if params.taker_fee_rate is None:
        raise ValueError("taker_fee_rate is required to report results after fees")
    rate = params.taker_fee_rate
    arms = {
        name: {"decisions": 0, "filled": ZERO, "requested": ZERO, "fees": ZERO, "value": ZERO}
        for name in ("fok", "limit_then_take")
    }
    for case in cases:
        d = case.decision
        for arm in arms.values():
            arm["decisions"] += 1
            arm["requested"] += d.shares
        # FOK arm: take the touch now, or die if depth is short.
        price = (
            case.book_at_decision.best_ask if d.side == "BUY" else case.book_at_decision.best_bid
        )
        if price and case.depth_at_touch >= d.shares:
            fok = arms["fok"]
            fok["filled"] += d.shares
            fok["fees"] += taker_fee(d.shares, price, rate)
            fok["value"] += _value(d, price, d.shares) - taker_fee(d.shares, price, rate)
        # Limit-then-take arm.
        ltt = arms["limit_then_take"]
        maker_px = (
            case.book_at_decision.best_bid if d.side == "BUY" else case.book_at_decision.best_ask
        )
        maker = min(case.attributed_maker_shares or ZERO, d.shares) if maker_px else ZERO
        if maker:
            ltt["filled"] += maker
            ltt["value"] += _value(d, maker_px, maker)
        remaining = d.shares - maker
        tp = case.book_at_timeout.best_ask if d.side == "BUY" else case.book_at_timeout.best_bid
        if (
            remaining > 0
            and tp
            and case.depth_at_timeout >= remaining
            and abs(tp - d.decision_price) / d.decision_price <= params.max_take_slippage
            and net_edge_per_share(d, tp, rate) > params.min_net_edge
        ):
            fee = taker_fee(remaining, tp, rate)
            ltt["filled"] += remaining
            ltt["fees"] += fee
            ltt["value"] += _value(d, tp, remaining) - fee
    return {
        "basis": (
            "fair_value minus fill price, after taker fees; maker fills only with attributed flow"
        ),
        "arms": {
            name: {
                "decisions": a["decisions"],
                "fill_rate": str(a["filled"] / a["requested"]) if a["requested"] else None,
                "fees_usdc": str(a["fees"]),
                "net_value_usdc": str(a["value"]),
            }
            for name, a in arms.items()
        },
    }
